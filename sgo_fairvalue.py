import argparse
import asyncio
import logging
import os
import statistics
import time

import requests

import devig

log = logging.getLogger("sgo_fairvalue")

SGO_API_BASE = "https://api.sportsgameodds.com/v2"


def sgo_get(path: str, params: dict) -> dict:
    api_key = os.environ.get("SGO_API_KEY")
    if not api_key:
        raise RuntimeError("set SGO_API_KEY (sportsgameodds.com API key)")
    response = requests.get(SGO_API_BASE + path, params=params,
                            headers={"x-api-key": api_key}, timeout=15)
    if not response.ok:
        raise RuntimeError(f"SGO HTTP {response.status_code}: "
                           f"{response.text[:300]}")
    return response.json()


def events_in(payload) -> list:
    if isinstance(payload, list):
        return payload
    for key in ("data", "events"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


class WatcherSGO:
    def __init__(self, event_id: str, odd_id: str,
                 max_age_seconds: float = 45.0, invert: bool = False,
                 strike_line=None):
        self.event_id = event_id
        self.odd_id = odd_id
        self.max_age_seconds = max_age_seconds
        self.invert = invert
        self.strike_line = float(strike_line) \
            if strike_line is not None \
            else None
        self.fair_probability = None
        self.consensus_line = None
        self.market_name = ""
        self.source = ""
        self.updated_at = 0.0

    def fresh_fair(self):
        if self.fair_probability is None:
            return None
        if time.time() - self.updated_at > self.max_age_seconds:
            return None
        return self.fair_probability

    def age_seconds(self) -> float:
        if not self.updated_at:
            return float("inf")
        return time.time() - self.updated_at

    def refresh(self):
        params = {"eventID": self.event_id, "oddID": self.odd_id,
                  "includeOpposingOdds": "true"}
        if self.strike_line is not None:
            params["includeAltLines"] = "true"
        events = events_in(sgo_get("/events", params))
        if not events:
            raise RuntimeError(f"event {self.event_id} not found")
        self.apply(events[0].get("odds", {}))

    def apply(self, event_odds: dict):
        odd = event_odds.get(self.odd_id)
        if odd is None:
            raise RuntimeError(
                f"oddID {self.odd_id} not on event "
                f"(try: python sgo_fairvalue.py odds {self.event_id})")
        self.market_name = odd.get("marketName", "")
        self.consensus_line = (odd.get("fairOverUnder")
                               or odd.get("bookOverUnder"))
        if odd.get("cancelled") or odd.get("ended"):
            self.fair_probability = None
            log.warning("SGO odd %s is %s",
                        self.odd_id,
                        "cancelled"
                        if odd.get("cancelled")
                        else "ended")
            return
        probability = self._fair_probability_from(odd, event_odds)
        if probability is not None:
            self.fair_probability = (1.0 - probability
                                     if self.invert
                                     else probability)
            self.updated_at = time.time()

    def _fair_probability_from(self, odd: dict, event_odds: dict):
        opposing_odd = event_odds.get(odd.get("opposingOddID") or "", {})
        if self.strike_line is not None:
            return self._fair_at_strike(odd, opposing_odd)
        if (odd.get("fairOdds") is not None
                and odd.get("fairOddsAvailable", True)):
            self.source = "fairOdds"
            return devig.implied_probability(odd["fairOdds"])
        consensus = (odd.get("bookOdds")
                     if odd.get("bookOddsAvailable", True)
                     else None)
        opposing_consensus = (opposing_odd.get("bookOdds")
                              if opposing_odd.get("bookOddsAvailable", True)
                              else None)
        if consensus is not None and opposing_consensus is not None:
            self.source = "devig(bookOdds)"
            return devig.remove_vig([consensus, opposing_consensus],
                                    "power")[0][0]
        log.warning("no usable open odds on %s (fair/book unavailable)",
                    self.odd_id)
        return None

    def _fair_at_strike(self, odd: dict, opposing_odd: dict):
        # TODO Reduce cognitive complexity
        strike = self.strike_line
        consensus_line = odd.get("fairOverUnder")
        if (consensus_line is not None
                and abs(float(consensus_line) - strike) < 1e-9
                and odd.get("fairOdds") is not None
                and odd.get("fairOddsAvailable", True)):
            self.source = f"fairOdds@{consensus_line}"
            self.consensus_line = consensus_line
            return devig.implied_probability(odd["fairOdds"])
        our_side = self._bookmaker_odds_at_strike(odd, strike)
        opposing_side = self._bookmaker_odds_at_strike(opposing_odd, strike)
        per_book_fairs = [
            devig.remove_vig([our_side[bookmaker], opposing_side[bookmaker]],
                             "power")[0][0]
            for bookmaker in our_side if bookmaker in opposing_side]
        if not per_book_fairs:
            lines_seen = set()
            for side in (odd, opposing_odd):
                for quote in (side.get("byBookmaker") or {}).values():
                    for candidate in [quote] + list(quote.get("altLines") or []):
                        if candidate.get("overUnder") is not None:
                            lines_seen.add(str(candidate["overUnder"]))
            log.warning("no bookmaker offers line %g on %s (lines seen: %s)",
                        strike, self.odd_id,
                        ", ".join(sorted(lines_seen)) or "none")
            return None
        self.source = f"altLines@{strike:g} ({len(per_book_fairs)} books)"
        self.consensus_line = f"{strike:g}"
        return statistics.median(per_book_fairs)

    @staticmethod
    def _bookmaker_odds_at_strike(odd: dict, strike: float) -> dict:
        odds_by_bookmaker = {}
        for bookmaker, quote in (odd.get("byBookmaker") or {}).items():
            for candidate in [quote] + list(quote.get("altLines") or []):
                candidate_line = candidate.get("overUnder")
                if (candidate_line is not None and candidate.get("available")
                        and candidate.get("odds") is not None
                        and abs(float(candidate_line) - strike) < 1e-9):
                    odds_by_bookmaker[bookmaker] = candidate["odds"]
                    break
        return odds_by_bookmaker


class EventPollerSGO:
    def __init__(self, event_id: str, poll_seconds: float = 10.0):
        self.event_id = event_id
        self.poll_seconds = poll_seconds
        self.watchers = []
        self._stop_requested = asyncio.Event()

    def watch(self, odd_id: str, strike_line=None, invert: bool = False,
              max_age_seconds: float = 45.0) -> WatcherSGO:
        watcher = WatcherSGO(self.event_id, odd_id,
                              max_age_seconds=max_age_seconds, invert=invert,
                              strike_line=strike_line)
        self.watchers.append(watcher)
        return watcher

    def refresh(self):
        for watcher in self.watchers:
            watcher.refresh()

    def stop(self):
        self._stop_requested.set()

    async def run(self):
        retry_delay_seconds = 1.0
        while not self._stop_requested.is_set():
            try:
                await asyncio.to_thread(self.refresh)
                retry_delay_seconds = 1.0
            except Exception as error:
                log.warning("SGO poll failed for %s (%s); retrying",
                            self.event_id, error)
                await asyncio.sleep(min(retry_delay_seconds, 30.0))
                retry_delay_seconds *= 2
            try:
                await asyncio.wait_for(self._stop_requested.wait(),
                                       timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass


def list_events(args):
    params = {"leagueID": args.league, "oddsAvailable": "true", "limit": 50}
    for event in events_in(sgo_get("/events", params)):
        teams = event.get("teams", {})
        matchup = " vs ".join(
            str(teams.get(side, {}).get("names", {}).get("long")
                or teams.get(side, {}).get("teamID", side))
            for side in ("away", "home")
        ) if teams \
            else ""
        row = (f"{event.get('eventID', ''):42s} {matchup}  "
               f"{event.get('status', {}).get('startsAt', '')}")
        if not args.search or args.search.lower() in row.lower():
            print(row)


def list_odds(args):
    events = events_in(sgo_get("/events", {"eventID": args.event_id}))
    if not events:
        raise SystemExit(f"event {args.event_id} not found")
    for odd_id, odd in sorted(events[0].get("odds", {}).items()):
        closed = not (odd.get("fairOddsAvailable", True)
                      or odd.get("bookOddsAvailable", True))
        row = (f"{odd_id:60s} fair={odd.get('fairOdds', '?'):>6} "
               f"book={odd.get('bookOdds', '?'):>6} "
               f"line={odd.get('fairOverUnder') or odd.get('bookOverUnder') or '-'}")
        if closed:
            row += "  [stale/closed]"
        if not args.grep or args.grep.lower() in odd_id.lower():
            print(row)


def watch_odd(args):
    watcher = WatcherSGO(args.event_id, args.odd_id, invert=args.invert,
                          strike_line=args.line)
    while True:
        watcher.refresh()
        print(f"{time.strftime('%H:%M:%S')}  "
              f"fair={watcher.fair_probability:.4f} "
              f"({watcher.fair_probability * 100:.1f}c YES)  "
              f"line={watcher.consensus_line}  [{watcher.source}]  "
              f"{watcher.market_name}")
        time.sleep(args.poll)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="SportsGameOdds fair-value tools. oddID format: "
                    "{statID}-{statEntityID}-{periodID}-{betTypeID}-{sideID}; "
                    "pick the side that settles YES on Kalshi.")
    commands = parser.add_subparsers(dest="command", required=True)
    events_parser = commands.add_parser("events",
                                        help="list events (find your eventID)")
    events_parser.add_argument("--league", default="NFL")
    events_parser.add_argument("--search", default=None)
    odds_parser = commands.add_parser("odds", help="list oddIDs on one event")
    odds_parser.add_argument("event_id")
    odds_parser.add_argument("--grep", default=None)
    watch_parser = commands.add_parser("watch",
                                       help="poll one odd and print its fair")
    watch_parser.add_argument("event_id")
    watch_parser.add_argument("odd_id")
    watch_parser.add_argument("--poll", type=float, default=10.0)
    watch_parser.add_argument("--line", default=None,
                              help="fair at this exact strike via alt lines")
    watch_parser.add_argument("--invert", action="store_true",
                              help="use 1-p (the odd settles Kalshi NO)")
    args = parser.parse_args()
    {"events": list_events, "odds": list_odds,
     "watch": watch_odd}[args.command](args)


if __name__ == "__main__":
    main()

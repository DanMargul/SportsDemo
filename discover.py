import argparse
import json
import logging

import kalshi
from market_catalog import MARKET_FAMILIES, parse_ticker
from event_matcher import rank_events
from odd_matcher import match_odd
from sgo_fairvalue import events_in, sgo_get

log = logging.getLogger("discover")

CONFIDENCE_REVIEW_THRESHOLD = 0.75


def enumerate_markets(client, series_ticker, event_ticker, status,
                      max_markets):
    raw = client.get_markets(series_ticker=series_ticker,
                             event_ticker=event_ticker, status=status,
                             max_markets=max_markets)
    known, skipped = [], []
    for market in raw:
        ticker = market.get("ticker", "")
        parsed = parse_ticker(ticker)
        if parsed.family is None:
            skipped.append(ticker)
        else:
            known.append((ticker, parsed))
    return known, skipped


def list_markets(args):
    client = kalshi.KalshiClient()
    known, skipped = enumerate_markets(
        client, args.series, args.event, args.status, args.max)
    print(f"matchable markets ({len(known)} of "
          f"{len(known) + len(skipped)} enumerated):")
    for ticker, parsed in known:
        detail = (f"strike {parsed.strike}" if parsed.family.has_strike
                  else f"side {parsed.side_code}")
        print(f"  {ticker:48} {parsed.family.kalshi_prefix:12} {detail}")
    if skipped:
        print(f"\n{len(skipped)} unmatchable (unknown family), e.g.:")
        for ticker in skipped[:5]:
            print(f"  {ticker}")


def scan(args):
    client = kalshi.KalshiClient()
    known, skipped = enumerate_markets(
        client, args.series, args.event, args.status, args.max)
    if not known:
        print("no matchable markets found for those filters")
        return
    print(f"scanning {len(known)} matchable markets "
          f"({len(skipped)} skipped)...\n")
    entries = []
    for ticker, _parsed in known:
        entry = propose(argparse.Namespace(ticker=ticker, search=args.search))
        if entry is not None:
            entries.append(entry)
    config = {"defaults": {"size": 5, "max_inventory": 20, "gamma": 0.1,
                           "k": 50, "sgo_poll": 10.0},
              "markets": entries}
    review_count = sum(1 for entry in entries if "_REVIEW" in entry)
    print("\n" + "=" * 60)
    print(f"DRAFT markets.json: {len(entries)} markets, "
          f"{review_count} need review")
    print("=" * 60)
    print(json.dumps(config, indent=2))
    if args.out:
        json.dump(config, open(args.out, "w"), indent=2)
        print(f"\nwritten to {args.out} -- REVIEW every _REVIEW entry "
              f"before --live")
    _persist_crosswalk()


def _persist_crosswalk():
    crosswalk = getattr(propose, "_crosswalk", None)
    if crosswalk:
        from player_crosswalk import save_crosswalk
        save_crosswalk(crosswalk)
        print(f"player crosswalk updated ({len(crosswalk)} entries cached)")


def list_families(args):
    print("Known Kalshi market families (curated):")
    for family in MARKET_FAMILIES:
        strike = "strike" if family.has_strike else "no-strike"
        player = ", player-prop" if family.has_player else ""
        print(f"  {family.kalshi_prefix:14} {family.league}  {strike}{player}"
              f"  -> SGO {family.sgo_stat_id}-*-game-{family.sgo_bet_type}-"
              f"{family.kalshi_yes_side}")
        print(f"       {family.description}")


def fetch_sgo_events(league, search):
    params = {"leagueID": league.upper(), "oddsAvailable": "true", "limit": 100}
    events = events_in(sgo_get("/events", params))
    if search:
        events = [event for event in events
                  if search.lower() in json.dumps(event).lower()]
    return events


def propose(args):
    parsed = parse_ticker(args.ticker)
    print(f"\n=== {args.ticker} ===")
    if parsed.family is None:
        print(f"  UNKNOWN market family '{parsed.prefix}'. Known families:")
        for family in MARKET_FAMILIES:
            print(f"    {family.kalshi_prefix}")
        return None
    print(f"  family : {parsed.family.description}")
    print(f"  parsed : date={parsed.date} game={parsed.game_number} "
          f"teams={'/'.join(parsed.team_codes)} strike={parsed.strike} "
          f"side={parsed.side_code or '-'} player={parsed.player_code or '-'}")
    for note in parsed.notes:
        print(f"  ! parse note: {note}")

    events = fetch_sgo_events(parsed.league, args.search)
    if not events:
        print("  no SGO events returned for this league/search")
        return None
    event_matches = rank_events(parsed, events)
    best_event = event_matches[0]

    print(f"\n  top event candidates:")
    for match in event_matches[:3]:
        marker = " <-- best" if match is best_event else ""
        print(f"    {match.sgo_event_id:20} conf={match.confidence}"
              f"  {'/'.join(c for c in match.sgo_teams if c)}{marker}")
        for concern in match.concerns:
            print(f"        ! {concern}")

    chosen_event = next(event for event in events
                        if event.get("eventID") == best_event.sgo_event_id)
    event_odds = chosen_event.get("odds", {})

    player_entity = None
    player_score = 1.0
    if parsed.family.has_player:
        from player_crosswalk import resolve_player
        crosswalk = getattr(propose, "_crosswalk", None)
        if crosswalk is None:
            from player_crosswalk import load_crosswalk
            crosswalk = load_crosswalk()
            propose._crosswalk = crosswalk
        player_entity, player_score, player_source, player_concerns = \
            resolve_player(parsed.player_code, set(parsed.team_codes),
                           parsed.family.sgo_stat_id, event_odds, crosswalk)
        print(f"\n  player resolution:")
        print(f"    {parsed.player_code} -> {player_entity} "
              f"(score {player_score}, {player_source})")
        for concern in player_concerns:
            print(f"    ! {concern}")
        if player_entity and player_source != "crosswalk":
            crosswalk[parsed.player_code] = player_entity

    odd = match_odd(parsed, event_odds, player_entity=player_entity)

    print(f"\n  proposed odd mapping:")
    print(f"    oddID  : {odd.sgo_odd_id}")
    print(f"    line   : {odd.sgo_line}   invert: {odd.invert}")
    for item in odd.evidence:
        print(f"    + {item}")
    for concern in odd.concerns:
        print(f"    ! {concern}")

    overall = round(min(best_event.confidence, odd.confidence), 3)
    weak_player = parsed.family.has_player and (
        player_entity is None or player_score < 0.85)
    needs_review = (overall < CONFIDENCE_REVIEW_THRESHOLD
                    or parsed.game_number > 1 or odd.invert or weak_player)
    print(f"\n  overall confidence: {overall}  "
          f"{'** NEEDS REVIEW **' if needs_review else 'looks clean'}")

    entry = {"ticker": args.ticker,
             "sgo_event": best_event.sgo_event_id,
             "sgo_odd": odd.sgo_odd_id}
    if odd.sgo_line is not None:
        entry["sgo_line"] = odd.sgo_line
    if odd.invert:
        entry["sgo_invert"] = True
    entry["_confidence"] = overall
    if needs_review:
        entry["_REVIEW"] = "; ".join(odd.concerns[:2]) or "low confidence"
    return entry


def draft_config(args):
    entries = []
    for ticker in args.tickers:
        entry = propose(argparse.Namespace(ticker=ticker, search=args.search))
        if entry is not None:
            entries.append(entry)
    config = {"defaults": {"size": 5, "max_inventory": 20, "gamma": 0.1,
                           "k": 50, "sgo_poll": 10.0},
              "markets": entries}
    print("\n" + "=" * 60)
    print("DRAFT markets.json (review every _REVIEW entry before --live):")
    print("=" * 60)
    print(json.dumps(config, indent=2))
    if args.out:
        json.dump(config, open(args.out, "w"), indent=2)
        print(f"\nwritten to {args.out} -- REVIEW before use")
    _persist_crosswalk()


def main():
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="Discover and match Kalshi tickers to SGO events/odds. "
                    "Proposes mappings with confidence and evidence; emits a "
                    "draft markets.json for human review. Never auto-commits "
                    "a live mapping.")
    commands = parser.add_subparsers(dest="command", required=True)

    families_parser = commands.add_parser(
        "families", help="list known Kalshi market families")
    families_parser.set_defaults(func=list_families)

    enum_parser = commands.add_parser(
        "enumerate", help="list matchable Kalshi markets (by series or event)")
    enum_parser.add_argument("--series", default=None,
                             help="Kalshi series ticker (e.g. KXMLBTOTAL)")
    enum_parser.add_argument("--event", default=None,
                             help="Kalshi event ticker (one game)")
    enum_parser.add_argument("--status", default="open")
    enum_parser.add_argument("--max", type=int, default=500)
    enum_parser.add_argument("--env", choices=["prod", "demo"], default=None)
    enum_parser.set_defaults(func=list_markets)

    scan_parser = commands.add_parser(
        "scan", help="enumerate Kalshi markets and propose SGO mappings for "
                     "all of them, emitting a draft config")
    scan_parser.add_argument("--series", default=None)
    scan_parser.add_argument("--event", default=None)
    scan_parser.add_argument("--status", default="open")
    scan_parser.add_argument("--max", type=int, default=100)
    scan_parser.add_argument("--search", default=None)
    scan_parser.add_argument("--out", default=None)
    scan_parser.add_argument("--env", choices=["prod", "demo"], default=None)
    scan_parser.set_defaults(func=scan)

    propose_parser = commands.add_parser(
        "propose", help="propose an SGO mapping for one Kalshi ticker")
    propose_parser.add_argument("ticker")
    propose_parser.add_argument("--search", default=None,
                                help="filter SGO events by substring")
    propose_parser.add_argument("--env", choices=["prod", "demo"], default=None)
    propose_parser.set_defaults(func=lambda a: propose(a))

    draft_parser = commands.add_parser(
        "draft", help="emit a draft markets.json for several tickers")
    draft_parser.add_argument("tickers", nargs="+")
    draft_parser.add_argument("--search", default=None)
    draft_parser.add_argument("--out", default=None,
                              help="write draft config to this path")
    draft_parser.add_argument("--env", choices=["prod", "demo"], default=None)
    draft_parser.set_defaults(func=draft_config)

    args = parser.parse_args()
    if getattr(args, "env", None):
        kalshi.environment = args.env
    args.func(args)


if __name__ == "__main__":
    main()

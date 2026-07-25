import argparse
import csv
import json
import logging

from sports_markets import kalshi
from sports_markets.market_catalog import MARKET_FAMILIES, parse_ticker, ticker_codes_for
from sports_markets.event_matcher import rank_events
from sports_markets.odd_matcher import match_odd
from sports_markets.sgo_fairvalue import events_in, sgo_get

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
    leagues = sorted({parsed.league for _ticker, parsed in known})
    events = []
    for league in leagues:
        events.extend(fetch_sgo_events(league, args.search))
    print(f"scanning {len(known)} matchable markets against "
          f"{len(events)} SGO events ({len(skipped)} skipped, "
          f"{len(leagues)} league(s), one SGO fetch per league)...\n")
    id_map = _id_map()
    entries = []
    for ticker, parsed in known:
        entry = propose_against(ticker, parsed, events, id_map)
        if entry is not None:
            entries.append(entry)
    review_count = sum(1 for entry in entries if "_REVIEW" in entry)
    print("\n" + "=" * 60)
    print(f"DRAFT: {len(entries)} markets, {review_count} need review")
    print("=" * 60)
    if args.out:
        write_entries(entries, args.out)
    else:
        print(json.dumps(
            {"defaults": dict(DEFAULT_CONFIG_DEFAULTS),
             "markets": [{k: v for k, v in e.items() if k != "_evidence"}
                         for e in entries]}, indent=2))
    _persist_id_map()


CSV_COLUMNS = ["ticker", "needs_review", "confidence", "review_notes",
               "sgo_event", "sgo_odd", "sgo_line", "sgo_invert",
               "player_decoded", "player_entity", "player_score",
               "kalshi_teams", "kalshi_date", "kalshi_time", "strike",
               "event_teams", "event_start", "event_confidence",
               "player_code"]

DEFAULT_CONFIG_DEFAULTS = {"size": 5, "max_inventory": 20, "gamma": 0.1,
                           "k": 50, "sgo_poll": 10.0}


def entry_to_row(entry):
    evidence = entry.get("_evidence", {})
    return {
        "ticker": entry["ticker"],
        "needs_review": "REVIEW" if "_REVIEW" in entry else "",
        "confidence": entry.get("_confidence", ""),
        "review_notes": entry.get("_REVIEW", ""),
        "sgo_event": entry.get("sgo_event", ""),
        "sgo_odd": entry.get("sgo_odd", ""),
        "sgo_line": entry.get("sgo_line", ""),
        "sgo_invert": "TRUE" if entry.get("sgo_invert") else "",
        "player_decoded": evidence.get("player_decoded", ""),
        "player_entity": evidence.get("player_entity", ""),
        "player_score": evidence.get("player_score", ""),
        "kalshi_teams": evidence.get("kalshi_teams", ""),
        "kalshi_date": evidence.get("kalshi_date", ""),
        "kalshi_time": evidence.get("kalshi_time", ""),
        "strike": evidence.get("strike", ""),
        "event_teams": evidence.get("event_teams", ""),
        "event_start": evidence.get("event_start", ""),
        "event_confidence": evidence.get("event_confidence", ""),
        "player_code": evidence.get("player_code", ""),
    }


def write_csv(entries, path):
    rows = [entry_to_row(entry) for entry in entries]
    rows.sort(key=lambda row: (row["needs_review"] != "REVIEW",
                               row["confidence"], row["ticker"]))
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def write_json_config(entries, path):
    config = {"defaults": dict(DEFAULT_CONFIG_DEFAULTS),
              "markets": [{key: value for key, value in entry.items()
                           if key != "_evidence"} for entry in entries]}
    json.dump(config, open(path, "w"), indent=2)
    return config


def write_entries(entries, path):
    if path.lower().endswith(".csv"):
        rows = write_csv(entries, path)
        flagged = sum(1 for row in rows if row["needs_review"])
        print(f"\nwrote {len(rows)} rows to {path} "
              f"({flagged} flagged REVIEW, sorted worst first)")
        print(f"review it, then: python discover.py build {path} "
              f"--out markets.json")
    else:
        write_json_config(entries, path)
        print(f"\nwritten to {path} -- REVIEW every _REVIEW entry before --live")


def build_config(args):
    with open(args.csv) as handle:
        rows = list(csv.DictReader(handle))
    kept, skipped = [], []
    for row in rows:
        if row.get("needs_review", "").strip() and not args.include_flagged:
            skipped.append(row)
            continue
        entry = {"ticker": row["ticker"].strip(),
                 "sgo_event": row["sgo_event"].strip(),
                 "sgo_odd": row["sgo_odd"].strip()}
        if not (entry["ticker"] and entry["sgo_event"] and entry["sgo_odd"]):
            skipped.append(row)
            continue
        if "PLAYER_UNKNOWN" in entry["sgo_odd"]:
            skipped.append(row)
            continue
        if row.get("sgo_line", "").strip():
            entry["sgo_line"] = float(row["sgo_line"])
        if row.get("sgo_invert", "").strip().upper() in {"TRUE", "1", "YES"}:
            entry["sgo_invert"] = True
        kept.append(entry)
    if not kept:
        raise SystemExit(
            f"no usable rows in {args.csv} "
            f"({len(skipped)} skipped; clear the needs_review column on rows "
            f"you have checked, or pass --include-flagged)")
    config = {"defaults": dict(DEFAULT_CONFIG_DEFAULTS), "markets": kept}
    json.dump(config, open(args.out, "w"), indent=2)
    print(f"wrote {len(kept)} markets to {args.out} ({len(skipped)} skipped)")


def _persist_id_map():
    id_map = getattr(_id_map, "_cache", None)
    if id_map:
        from player_id_map import save_id_map
        save_id_map(id_map)
        print(f"player ID map updated ({len(id_map)} entries cached)")


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


def _id_map():
    cache = getattr(_id_map, "_cache", None)
    if cache is None:
        from player_id_map import load_id_map
        cache = load_id_map()
        _id_map._cache = cache
    return cache


def propose(args):
    if "-" not in args.ticker:
        print(f"'{args.ticker}' looks like a series prefix, not a full ticker.")
        print(f"To match every market in that series, use scan:")
        print(f"    python discover.py scan --series {args.ticker} --out draft.json")
        print(f"To match one market, pass its full ticker, e.g.:")
        print(f"    python discover.py propose {args.ticker}-<GAME>-<SUFFIX>")
        return None
    parsed = parse_ticker(args.ticker)
    if parsed.family is None:
        print(f"\n=== {args.ticker} ===")
        print(f"  UNKNOWN market family '{parsed.prefix}'. Known families:")
        for family in MARKET_FAMILIES:
            print(f"    {family.kalshi_prefix}")
        return None
    events = fetch_sgo_events(parsed.league, args.search)
    if not events:
        print(f"\n=== {args.ticker} ===")
        print("  no SGO events returned for this league/search")
        return None
    return propose_against(args.ticker, parsed, events, _id_map())


def propose_against(ticker, parsed, events, id_map):
    print(f"\n=== {ticker} ===")
    print(f"  family : {parsed.family.description}")
    print(f"  parsed : date={parsed.date} game={parsed.game_number} "
          f"teams={'/'.join(parsed.team_codes)} strike={parsed.strike} "
          f"side={parsed.side_code or '-'} player={parsed.player_code or '-'}")
    for note in parsed.notes:
        print(f"  ! parse note: {note}")

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
    player_concerns = []
    if parsed.family.has_player:
        from player_id_map import resolve_player
        player_entity, player_score, player_source, player_concerns = \
            resolve_player(parsed.player_code,
                           ticker_codes_for(parsed.league, parsed.team_codes),
                           parsed.family.sgo_stat_id, event_odds, id_map)
        print(f"\n  player resolution:")
        print(f"    {parsed.player_code} -> {player_entity} "
              f"(score {player_score}, {player_source})")
        for concern in player_concerns:
            print(f"    ! {concern}")
        if player_entity and player_source != "id-map":
            id_map[parsed.player_code] = player_entity

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
        player_entity is None or player_score < 0.85
        or bool(player_concerns))
    needs_review = (overall < CONFIDENCE_REVIEW_THRESHOLD
                    or parsed.game_number > 1 or odd.invert or weak_player)
    print(f"\n  overall confidence: {overall}  "
          f"{'** NEEDS REVIEW **' if needs_review else 'looks clean'}")

    entry = {"ticker": ticker,
             "sgo_event": best_event.sgo_event_id,
             "sgo_odd": odd.sgo_odd_id}
    if odd.sgo_line is not None:
        entry["sgo_line"] = odd.sgo_line
    if odd.invert:
        entry["sgo_invert"] = True
    entry["_confidence"] = overall
    if needs_review:
        entry["_REVIEW"] = "; ".join(
            list(player_concerns) + list(odd.concerns[:2])) or "low confidence"
    entry["_evidence"] = {
        "kalshi_date": parsed.date,
        "kalshi_time": parsed.time_hhmm,
        "kalshi_teams": "/".join(parsed.team_codes),
        "strike": parsed.strike,
        "event_confidence": best_event.confidence,
        "event_teams": "/".join(code for code in best_event.sgo_teams if code),
        "event_start": best_event.sgo_start,
        "player_code": parsed.player_code,
        "player_decoded": _decoded_player_display(parsed),
        "player_entity": player_entity or "",
        "player_score": player_score if parsed.family.has_player else "",
    }
    return entry


def _decoded_player_display(parsed):
    if not parsed.family.has_player or not parsed.player_code:
        return ""
    from player_codes import decode_player_code
    decoded = decode_player_code(
        parsed.player_code, ticker_codes_for(parsed.league, parsed.team_codes))
    return f"{decoded.first_initial}. {decoded.last_name.title()} #{decoded.number}"


def draft_config(args):
    parsed_list = [(ticker, parse_ticker(ticker)) for ticker in args.tickers]
    parsed_list = [(t, p) for t, p in parsed_list if p.family is not None]
    leagues = sorted({p.league for _t, p in parsed_list})
    events = []
    for league in leagues:
        events.extend(fetch_sgo_events(league, args.search))
    id_map = _id_map()
    entries = []
    for ticker, parsed in parsed_list:
        entry = propose_against(ticker, parsed, events, id_map)
        if entry is not None:
            entries.append(entry)
    review_count = sum(1 for entry in entries if "_REVIEW" in entry)
    print("\n" + "=" * 60)
    print(f"DRAFT: {len(entries)} markets, {review_count} need review")
    print("=" * 60)
    if args.out:
        write_entries(entries, args.out)
    else:
        print(json.dumps(
            {"defaults": dict(DEFAULT_CONFIG_DEFAULTS),
             "markets": [{k: v for k, v in e.items() if k != "_evidence"}
                         for e in entries]}, indent=2))
    _persist_id_map()


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

    build_parser = commands.add_parser(
        "build", help="turn a reviewed CSV into a runnable markets.json")
    build_parser.add_argument("csv")
    build_parser.add_argument("--out", default="markets.json")
    build_parser.add_argument("--include-flagged", action="store_true",
                              help="include rows still marked needs_review")
    build_parser.set_defaults(func=build_config)

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

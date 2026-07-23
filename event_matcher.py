from dataclasses import dataclass, field

from market_catalog import canonical_team, parse_iso_date

EVENT_LOCAL_TIMEZONE = "America/New_York"


def to_event_local(moment):
    if moment is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo(EVENT_LOCAL_TIMEZONE))
    except Exception:
        return moment


def start_time_gap_minutes(parsed_ticker, local_dt):
    text = getattr(parsed_ticker, "time_hhmm", "")
    if not text or len(text) != 4 or not text.isdigit() or local_dt is None:
        return None
    ticker_minutes = int(text[:2]) * 60 + int(text[2:])
    local_minutes = local_dt.hour * 60 + local_dt.minute
    gap = abs(ticker_minutes - local_minutes)
    return min(gap, 24 * 60 - gap)


@dataclass
class EventMatch:
    sgo_event_id: str
    confidence: float
    evidence: list = field(default_factory=list)
    concerns: list = field(default_factory=list)
    sgo_teams: tuple = ()
    sgo_start: str = ""


def _sgo_event_team_codes(league, sgo_event):
    teams = sgo_event.get("teams", {})
    codes = []
    for side in ("away", "home"):
        block = teams.get(side, {})
        name = (block.get("names", {}).get("long")
                or block.get("names", {}).get("short")
                or block.get("teamID") or "")
        code = canonical_team(league, name)
        codes.append(code)
    return tuple(codes)


def score_event(parsed_ticker, sgo_event):
    evidence, concerns = [], []
    confidence = 0.0

    league = parsed_ticker.league
    sgo_codes = _sgo_event_team_codes(league, sgo_event)
    ticker_codes = set(parsed_ticker.team_codes)
    sgo_set = set(code for code in sgo_codes if code)

    if ticker_codes and sgo_set:
        overlap = ticker_codes & sgo_set
        if overlap == ticker_codes and len(ticker_codes) == len(sgo_set):
            confidence += 0.6
            evidence.append(f"teams match exactly ({'/'.join(sorted(overlap))})")
        elif overlap:
            confidence += 0.3 * (len(overlap) / len(ticker_codes))
            concerns.append(
                f"partial team match: ticker {sorted(ticker_codes)} "
                f"vs SGO {sorted(sgo_set)}")
        else:
            concerns.append(
                f"no team overlap: ticker {sorted(ticker_codes)} "
                f"vs SGO {sorted(sgo_set)}")
    else:
        concerns.append("could not resolve teams on one side")

    ticker_date = parsed_ticker.date
    sgo_start = (sgo_event.get("status", {}).get("startsAt")
                 or sgo_event.get("startsAt") or "")
    sgo_dt = parse_iso_date(sgo_start)
    local_dt = to_event_local(sgo_dt)
    if ticker_date and local_dt is not None:
        if local_dt.date().isoformat() == ticker_date:
            confidence += 0.3
            evidence.append(
                f"date matches ({ticker_date} local, "
                f"{sgo_dt.date().isoformat()} UTC)")
            minutes_apart = start_time_gap_minutes(parsed_ticker, local_dt)
            if minutes_apart is not None and minutes_apart <= 60:
                confidence += 0.1
                evidence.append(
                    f"start time matches ({parsed_ticker.time_hhmm} local, "
                    f"{minutes_apart:.0f}m apart)")
            elif minutes_apart is not None:
                concerns.append(
                    f"start time differs by {minutes_apart:.0f}m from ticker "
                    f"{parsed_ticker.time_hhmm} -- wrong game of a "
                    f"doubleheader?")
        elif abs((local_dt.date() -
                  parse_iso_date(ticker_date + "T00:00:00Z").date()).days) <= 1:
            confidence += 0.1
            concerns.append(
                f"date off by a day even after local conversion: ticker "
                f"{ticker_date} vs {local_dt.date().isoformat()}")
        else:
            concerns.append(
                f"date mismatch: ticker {ticker_date} vs "
                f"{local_dt.date().isoformat()}")
    else:
        concerns.append("could not compare dates")

    if parsed_ticker.game_number > 1:
        concerns.append(
            f"ticker is game {parsed_ticker.game_number} of a doubleheader; "
            f"verify start time -- same teams play twice this day")
        confidence = min(confidence, 0.85)

    return EventMatch(
        sgo_event_id=sgo_event.get("eventID", ""),
        confidence=round(min(confidence, 1.0), 3),
        evidence=evidence, concerns=concerns,
        sgo_teams=sgo_codes, sgo_start=sgo_start)


def rank_events(parsed_ticker, sgo_events):
    matches = [score_event(parsed_ticker, event) for event in sgo_events]
    matches.sort(key=lambda match: match.confidence, reverse=True)
    return matches

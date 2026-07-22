import re
from dataclasses import dataclass, field
from datetime import datetime, timezone


TEAM_ALIASES = {
    "mlb": {
        "ARI": {"arizona diamondbacks", "diamondbacks", "dbacks", "ari", "az"},
        "ATL": {"atlanta braves", "braves", "atl"},
        "BAL": {"baltimore orioles", "orioles", "bal"},
        "BOS": {"boston red sox", "red sox", "bos"},
        "CHC": {"chicago cubs", "cubs", "chc"},
        "CWS": {"chicago white sox", "white sox", "cws", "chw"},
        "CIN": {"cincinnati reds", "reds", "cin"},
        "CLE": {"cleveland guardians", "guardians", "cle"},
        "COL": {"colorado rockies", "rockies", "col"},
        "DET": {"detroit tigers", "tigers", "det"},
        "HOU": {"houston astros", "astros", "hou"},
        "KC": {"kansas city royals", "royals", "kc", "kcr"},
        "LAA": {"los angeles angels", "angels", "la angels", "laa", "ana"},
        "LAD": {"los angeles dodgers", "dodgers", "la dodgers", "lad"},
        "MIA": {"miami marlins", "marlins", "mia", "fla"},
        "MIL": {"milwaukee brewers", "brewers", "mil"},
        "MIN": {"minnesota twins", "twins", "min"},
        "NYM": {"new york mets", "mets", "ny mets", "nym"},
        "NYY": {"new york yankees", "yankees", "ny yankees", "nyy"},
        "ATH": {"athletics", "oakland athletics", "as", "ath", "oak"},
        "PHI": {"philadelphia phillies", "phillies", "phi"},
        "PIT": {"pittsburgh pirates", "pirates", "pit"},
        "SD": {"san diego padres", "padres", "sd", "sdp"},
        "SF": {"san francisco giants", "giants", "sf", "sfg"},
        "SEA": {"seattle mariners", "mariners", "sea"},
        "STL": {"st louis cardinals", "st. louis cardinals", "cardinals", "stl"},
        "TB": {"tampa bay rays", "rays", "tb", "tbr"},
        "TEX": {"texas rangers", "rangers", "tex"},
        "TOR": {"toronto blue jays", "blue jays", "tor"},
        "WSH": {"washington nationals", "nationals", "wsh", "was", "wsn"},
    },
}


def canonical_team(league: str, name_or_code: str):
    text = str(name_or_code).strip().lower()
    table = TEAM_ALIASES.get(league.lower(), {})
    for code, aliases in table.items():
        if text == code.lower() or text in aliases:
            return code
    for code, aliases in table.items():
        if any(alias in text or text in alias for alias in aliases):
            return code
    return None


@dataclass
class MarketFamily:
    kalshi_prefix: str
    league: str
    description: str
    sgo_stat_id: str
    sgo_bet_type: str
    sgo_stat_entity: str
    kalshi_yes_side: str
    has_strike: bool
    has_player: bool = False


MARKET_FAMILIES = [
    MarketFamily("KXMLBTOTAL", "mlb", "game total runs over/under",
                 "points", "ou", "all", "over", True, has_player=False),
    MarketFamily("KXMLBGAME", "mlb", "game moneyline (which team wins)",
                 "points", "ml", "home", "home", False, has_player=False),
    MarketFamily("KXMLBHRR", "mlb", "player hits+runs+RBIs over/under",
                 "batting_hits+runs+rbi", "ou", "player", "over", True,
                 has_player=True),
]


def family_for_prefix(prefix: str):
    for family in MARKET_FAMILIES:
        if family.kalshi_prefix == prefix:
            return family
    return None


@dataclass
class ParsedTicker:
    ticker: str
    prefix: str = ""
    league: str = ""
    date: str = ""
    game_number: int = 1
    team_codes: tuple = field(default_factory=tuple)
    strike: float = None
    side_code: str = ""
    player_code: str = ""
    time_hhmm: str = ""
    family: MarketFamily = None
    notes: list = field(default_factory=list)


_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _parse_date_teams(middle: str, league: str):
    match = re.match(r"(\d{2})([A-Z]{3})(\d{2})(\d{4})([A-Z]+?)(G\d)?$", middle)
    if not match:
        return None, 1, (), ""
    year2, month_abbr, day, start_time, teams_blob, game_tag = match.groups()
    if month_abbr not in _MONTHS:
        return None, 1, (), ""
    iso_date = f"20{year2}-{_MONTHS[month_abbr]:02d}-{int(day):02d}"
    game_number = int(game_tag[1:]) if game_tag else 1
    codes = _split_team_blob(teams_blob, league)
    return iso_date, game_number, codes, start_time


def _ticker_code_map(league: str):
    table = TEAM_ALIASES.get(league.lower(), {})
    code_map = {}
    for canonical, aliases in table.items():
        code_map[canonical.upper()] = canonical
        for alias in aliases:
            if alias.isalpha() and 2 <= len(alias) <= 3:
                code_map[alias.upper()] = canonical
    return code_map


def ticker_team_codes(league: str):
    return _ticker_code_map(league)


def ticker_codes_for(league: str, canonical_codes) -> set:
    code_map = _ticker_code_map(league)
    wanted = set(canonical_codes)
    return {code for code, canonical in code_map.items()
            if canonical in wanted} | wanted


def _split_team_blob(blob: str, league: str):
    code_map = _ticker_code_map(league)
    known = sorted(code_map, key=len, reverse=True)
    found = []
    remaining = blob
    while remaining:
        for code in known:
            if remaining.startswith(code):
                found.append(code_map[code])
                remaining = remaining[len(code):]
                break
        else:
            remaining = remaining[1:]
    return tuple(found)


def parse_ticker(ticker: str) -> ParsedTicker:
    parsed = ParsedTicker(ticker=ticker)
    parts = ticker.split("-")
    parsed.prefix = parts[0]
    parsed.family = family_for_prefix(parsed.prefix)
    if parsed.family is None:
        parsed.notes.append(f"unknown market family: {parsed.prefix}")
        return parsed
    parsed.league = parsed.family.league

    if len(parts) >= 2:
        iso_date, game_number, codes, start_time = _parse_date_teams(
            parts[1], parsed.league)
        parsed.date = iso_date or ""
        parsed.game_number = game_number
        parsed.team_codes = codes
        parsed.time_hhmm = start_time
        if iso_date is None:
            parsed.notes.append(f"could not parse date/teams from {parts[1]}")

    trailing = parts[2:]
    if parsed.family.has_player and trailing:
        parsed.player_code = trailing[0]
        trailing = trailing[1:]

    if parsed.family.has_strike:
        if trailing:
            strike = _strike_from_suffix(trailing[-1])
            if strike is not None:
                parsed.strike = strike
            else:
                parsed.notes.append(
                    f"could not parse strike from {trailing[-1]}")
        else:
            parsed.notes.append("expected a strike segment, found none")
    elif trailing:
        resolved = canonical_team(parsed.league, trailing[-1])
        parsed.side_code = resolved or trailing[-1]
    return parsed




def _strike_from_suffix(suffix: str):
    digits = re.sub(r"[^\d.]", "", suffix)
    if not digits:
        return None
    value = float(digits)
    if value == int(value):
        return value - 0.5
    return value


def parse_iso_date(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None

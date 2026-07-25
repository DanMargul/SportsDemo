import re
from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass
class DecodedPlayer:
    raw: str
    team: str
    first_initial: str
    last_name: str
    number: str
    display: str

    def match_key(self):
        return (self.first_initial + self.last_name).upper()


def decode_player_code(code: str, team_codes) -> DecodedPlayer:
    team = ""
    remainder = code
    for candidate in sorted(team_codes, key=len, reverse=True):
        if code.startswith(candidate):
            team = candidate
            remainder = code[len(candidate):]
            break
    match = re.match(r"([A-Z])([A-Z]+?)(\d*)$", remainder)
    if not match:
        return DecodedPlayer(code, team, "", remainder, "", remainder)
    first_initial, last_name, number = match.groups()
    display = f"{first_initial}. {last_name.title()}"
    return DecodedPlayer(raw=code, team=team, first_initial=first_initial,
                         last_name=last_name, number=number, display=display)


def name_tokens(text: str) -> list:
    import unicodedata
    decomposed = unicodedata.normalize("NFKD", str(text))
    ascii_only = "".join(character for character in decomposed
                         if not unicodedata.combining(character))
    upper = ascii_only.upper()
    for joined in "-'.":
        upper = upper.replace(joined, "")
    for separated in "_+/,":
        upper = upper.replace(separated, " ")
    return [token for token in upper.split() if token]


def name_similarity(decoded: DecodedPlayer, full_name: str) -> float:
    tokens = name_tokens(full_name)
    if not tokens or not decoded.last_name:
        return 0.0
    match = _locate_surname(tokens, decoded.last_name)
    if match is None:
        return 0.0
    return round(_first_initial_adjustment(decoded, tokens, match.index,
                                           match.base_score), 3)


SURNAME_NEAR_MATCH_RATIO = 0.85
MAX_SURNAME_TOKENS = 4
EXACT_SURNAME_SCORE = 0.7
NEAR_SURNAME_SCORE = 0.6


@dataclass
class SurnameMatch:
    index: int
    exact: bool

    @property
    def base_score(self) -> float:
        return EXACT_SURNAME_SCORE if self.exact else NEAR_SURNAME_SCORE


def _candidate_surnames(tokens):
    for start in range(len(tokens)):
        for span in range(1, min(MAX_SURNAME_TOKENS,
                                 len(tokens) - start) + 1):
            yield start, "".join(tokens[start:start + span])


def _locate_surname(tokens, last_name):
    for start, joined in _candidate_surnames(tokens):
        if joined == last_name:
            return SurnameMatch(start, exact=True)
    return _best_near_surname(tokens, last_name)


def _best_near_surname(tokens, last_name):
    scored = [(SequenceMatcher(None, joined, last_name).ratio(), start)
              for start, joined in _candidate_surnames(tokens)
              if len(joined) >= 4]
    if not scored:
        return None
    ratio, start = max(scored)
    if ratio < SURNAME_NEAR_MATCH_RATIO:
        return None
    return SurnameMatch(start, exact=False)


def _first_initial_adjustment(decoded, tokens, surname_index, base_score):
    if surname_index == 0:
        return base_score - 0.1
    preceding = tokens[surname_index - 1]
    if preceding.startswith(decoded.first_initial):
        return base_score + 0.3
    return base_score / 2.0

from dataclasses import dataclass, field


@dataclass
class OddMatch:
    sgo_odd_id: str
    sgo_line: float
    invert: bool
    confidence: float
    evidence: list = field(default_factory=list)
    concerns: list = field(default_factory=list)


def build_odd_id(family, parsed_ticker, player_entity=None):
    entity = family.sgo_stat_entity
    if family.has_player:
        entity = player_entity or "PLAYER_UNKNOWN"
    return (f"{family.sgo_stat_id}-{entity}-game-"
            f"{family.sgo_bet_type}-{family.kalshi_yes_side}")


def match_odd(parsed_ticker, sgo_event_odds, player_entity=None):
    family = parsed_ticker.family
    evidence, concerns = [], []
    confidence = 0.5

    if family.has_player:
        if player_entity is None:
            concerns.append(
                f"player prop: could not resolve SGO statEntityID for "
                f"'{parsed_ticker.player_code}' -- MUST be set by hand")
            confidence = 0.2
        else:
            evidence.append(f"player resolved to {player_entity}")

    candidate_id = build_odd_id(family, parsed_ticker, player_entity)
    present = candidate_id in (sgo_event_odds or {})
    if present:
        confidence += 0.3
        evidence.append(f"oddID present on event: {candidate_id}")
    elif family.has_player and player_entity is None:
        pass
    else:
        concerns.append(
            f"oddID {candidate_id} not found on event -- "
            f"side/period/statID mapping may be wrong")
        confidence = min(confidence, 0.35)

    invert = False
    if family.kalshi_yes_side == "over":
        evidence.append("Kalshi YES = OVER -> SGO over side (no invert)")
    elif family.kalshi_yes_side == "home":
        if parsed_ticker.side_code and parsed_ticker.team_codes:
            home_code = parsed_ticker.team_codes[-1]
            if parsed_ticker.side_code == home_code:
                evidence.append(
                    f"Kalshi YES = {parsed_ticker.side_code} = home (no invert)")
            else:
                invert = True
                concerns.append(
                    f"Kalshi YES = {parsed_ticker.side_code} = AWAY; "
                    f"using invert (1-p) -- VERIFY this is correct")
        else:
            concerns.append("moneyline: could not determine home/away side")
            confidence = min(confidence, 0.4)

    concerns.append(
        "SETTLEMENT: confirm the SGO market settles on the same rule as "
        "Kalshi (regulation vs full game, ET/pens, etc.) before trusting")

    line = parsed_ticker.strike
    if family.has_strike and line is None:
        concerns.append("strike-bearing market but no strike parsed")
        confidence = min(confidence, 0.3)

    return OddMatch(
        sgo_odd_id=candidate_id, sgo_line=line, invert=invert,
        confidence=round(max(min(confidence, 1.0), 0.0), 3),
        evidence=evidence, concerns=concerns)

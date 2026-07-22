import json
import os
import re

from player_codes import decode_player_code, name_similarity

ID_MAP_PATH = "player_id_map.json"
PLAYER_MATCH_THRESHOLD = 0.7


def load_id_map(path=ID_MAP_PATH):
    if os.path.exists(path):
        return json.load(open(path))
    return {}


def save_id_map(id_map, path=ID_MAP_PATH):
    json.dump(id_map, open(path, "w"), indent=2, sort_keys=True)


def entity_ids_in_event(event_odds, stat_id):
    entities = {}
    prefix = stat_id + "-"
    for odd_id, odd in (event_odds or {}).items():
        if not odd_id.startswith(prefix):
            continue
        parts = odd_id.split("-")
        if len(parts) < 5:
            continue
        entity_id = parts[-4]
        name = (odd.get("statEntityName") or odd.get("playerName")
                or odd.get("marketName", ""))
        entities[entity_id] = name
    return entities


def resolve_player(player_code, team_codes, stat_id, event_odds,
                   id_map=None):
    id_map = id_map if id_map is not None else {}
    if player_code in id_map:
        return id_map[player_code], 1.0, "id-map", []

    decoded = decode_player_code(player_code, team_codes)
    candidates = entity_ids_in_event(event_odds, stat_id)
    if not candidates:
        return None, 0.0, "no-player-odds-on-event", [
            f"event has no {stat_id} player odds to match against"]

    scored = []
    for entity_id, name in candidates.items():
        score = name_similarity(decoded, entity_id)
        if name:
            score = max(score, name_similarity(decoded, name))
        scored.append((score, entity_id, name))
    scored.sort(reverse=True)
    best_score, best_id, best_name = scored[0]

    concerns = []
    if best_score < PLAYER_MATCH_THRESHOLD:
        concerns.append(
            f"best in-game match for '{decoded.display}' is {best_id} "
            f"(score {best_score}) -- below threshold, confirm by hand")
        return None, best_score, "below-threshold", concerns
    if len(scored) > 1 and scored[1][0] >= best_score - 0.15:
        concerns.append(
            f"ambiguous: {best_id} ({best_score}) vs {scored[1][1]} "
            f"({scored[1][0]}) -- confirm which is '{decoded.display}'")
    return best_id, best_score, "matched-in-event", concerns


def _score_against_entity_id(decoded, entity_id):
    upper = entity_id.upper()
    score = 0.0
    if decoded.last_name in upper:
        score += 0.6
    letters = re.sub(r"[^A-Z]", "", upper)
    if letters.startswith(decoded.first_initial):
        score += 0.2
    return round(score, 3)

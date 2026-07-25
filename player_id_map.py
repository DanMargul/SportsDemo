import json
import logging
import os
from datetime import datetime, timezone

from player_codes import decode_player_code, name_similarity

log = logging.getLogger("player_id_map")

ID_MAP_PATH = "player_id_map.json"
PLAYER_MATCH_THRESHOLD = 0.7


PLAYER_UPSERT = """
INSERT INTO players (kalshi_code, sgo_entity_id, display_name, team_code,
                     jersey_number, match_score, match_source,
                     source_event_id, confirmed_at, confirmed_by)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (kalshi_code) DO UPDATE SET
    sgo_entity_id = EXCLUDED.sgo_entity_id,
    display_name = COALESCE(EXCLUDED.display_name, players.display_name),
    team_code = COALESCE(EXCLUDED.team_code, players.team_code),
    jersey_number = COALESCE(EXCLUDED.jersey_number, players.jersey_number),
    match_score = EXCLUDED.match_score,
    match_source = EXCLUDED.match_source,
    source_event_id = EXCLUDED.source_event_id,
    confirmed_at = EXCLUDED.confirmed_at,
    confirmed_by = EXCLUDED.confirmed_by
"""


class PostgresPlayerIdMap:
    def __init__(self, connection):
        self.connection = connection
        self.entries = self.load_all()

    def __contains__(self, kalshi_code):
        return kalshi_code in self.entries

    def __getitem__(self, kalshi_code):
        return self.entries[kalshi_code]

    def __len__(self):
        return len(self.entries)

    def get(self, kalshi_code, default=None):
        return self.entries.get(kalshi_code, default)

    def load_all(self):
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT kalshi_code, sgo_entity_id FROM players")
            return {code: entity for code, entity in cursor.fetchall()}

    def warn_on_conflict(self, kalshi_code, sgo_entity_id):
        existing = self.entries.get(kalshi_code)
        if existing and existing != sgo_entity_id:
            log.warning("player %s was mapped to %s, now recording %s -- "
                        "confirm which is correct", kalshi_code, existing,
                        sgo_entity_id)

    def record(self, kalshi_code, sgo_entity_id, **provenance):
        self.warn_on_conflict(kalshi_code, sgo_entity_id)
        with self.connection.cursor() as cursor:
            cursor.execute(PLAYER_UPSERT, (
                kalshi_code, sgo_entity_id,
                provenance.get("display_name"),
                provenance.get("team_code"),
                provenance.get("jersey_number"),
                provenance.get("match_score"),
                provenance.get("match_source"),
                provenance.get("source_event_id"),
                provenance.get("confirmed_at")
                or datetime.now(timezone.utc),
                provenance.get("confirmed_by") or os.environ.get("USER")))
        self.connection.commit()
        self.entries[kalshi_code] = sgo_entity_id

    def close(self):
        try:
            self.connection.close()
        except Exception:
            pass


def open_player_id_map(url=None):
    import db
    return PostgresPlayerIdMap(db.connect(url))


def import_json_into_postgres(connection, path=ID_MAP_PATH):
    with open(path) as handle:
        entries = json.load(handle)
    store = PostgresPlayerIdMap(connection)
    imported = 0
    for kalshi_code, sgo_entity_id in sorted(entries.items()):
        if store.get(kalshi_code) == sgo_entity_id:
            continue
        store.record(kalshi_code, sgo_entity_id, match_source="imported-json",
                     confirmed_by="import")
        imported += 1
    return imported, len(entries)


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

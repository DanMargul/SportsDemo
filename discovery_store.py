import logging
import os
from datetime import datetime, timezone

from market_catalog import parse_ticker, parse_iso_date

log = logging.getLogger("discovery_store")

GAME_UPSERT = """
INSERT INTO games (league, sgo_event_id, home_team, away_team,
                   scheduled_start)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (sgo_event_id) DO UPDATE SET
    league = COALESCE(EXCLUDED.league, games.league),
    home_team = COALESCE(EXCLUDED.home_team, games.home_team),
    away_team = COALESCE(EXCLUDED.away_team, games.away_team),
    scheduled_start = COALESCE(EXCLUDED.scheduled_start,
                               games.scheduled_start)
RETURNING id
"""

MARKET_UPSERT = """
INSERT INTO markets (ticker, game_id, player_id, series_ticker, family,
                     league, strike, side_code, game_start)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (ticker) DO UPDATE SET
    game_id = COALESCE(EXCLUDED.game_id, markets.game_id),
    player_id = COALESCE(EXCLUDED.player_id, markets.player_id),
    series_ticker = COALESCE(EXCLUDED.series_ticker, markets.series_ticker),
    family = COALESCE(EXCLUDED.family, markets.family),
    league = COALESCE(EXCLUDED.league, markets.league),
    strike = COALESCE(EXCLUDED.strike, markets.strike),
    side_code = COALESCE(EXCLUDED.side_code, markets.side_code),
    game_start = COALESCE(EXCLUDED.game_start, markets.game_start)
RETURNING id
"""

CURRENT_MAPPING = """
SELECT id, sgo_event_id, sgo_odd_id, sgo_line, invert, review_state
FROM sgo_mappings WHERE market_id = %s AND is_current
"""

MAPPING_INSERT = """
INSERT INTO sgo_mappings (market_id, sgo_event_id, sgo_odd_id, sgo_line,
                          invert, confidence, review_state, review_notes)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
RETURNING id
"""

APPROVED_MAPPINGS = """
SELECT m.ticker, s.sgo_event_id, s.sgo_odd_id, s.sgo_line, s.invert
FROM sgo_mappings s
JOIN markets m ON m.id = s.market_id
WHERE s.is_current AND s.review_state = 'approved'
  AND s.sgo_odd_id NOT LIKE '%%PLAYER_UNKNOWN%%'
ORDER BY m.ticker
"""


def open_store(url=None):
    if not (url or os.environ.get("DATABASE_URL")):
        return None
    try:
        import db
        return DiscoveryStore(db.connect(url))
    except Exception as error:
        log.warning("could not open the discovery tables (%s); "
                    "writing files only", error)
        return None


def teams_from_evidence(evidence):
    text = (evidence or {}).get("event_teams") or ""
    parts = [part for part in text.split("/") if part]
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, None


def same_mapping(existing, entry):
    _id, event_id, odd_id, line, invert, _state = existing
    entry_line = entry.get("sgo_line")
    return (event_id == entry.get("sgo_event")
            and odd_id == entry.get("sgo_odd")
            and (float(line) if line is not None else None)
            == (float(entry_line) if entry_line is not None else None)
            and bool(invert) == bool(entry.get("sgo_invert")))


class DiscoveryStore:
    def __init__(self, connection):
        self.connection = connection
        self.markets_written = 0
        self.mappings_written = 0
        self.mappings_unchanged = 0

    def close(self):
        try:
            self.connection.close()
        except Exception:
            pass

    def upsert_game(self, entry):
        event_id = entry.get("sgo_event")
        evidence = entry.get("_evidence", {})
        if not event_id:
            return None
        away, home = teams_from_evidence(evidence)
        parsed = parse_ticker(entry["ticker"])
        league = getattr(parsed.family, "league", None)
        start = parse_iso_date(evidence.get("event_start"))
        with self.connection.cursor() as cursor:
            cursor.execute(GAME_UPSERT, (league, event_id, home, away, start))
            return cursor.fetchone()[0]

    def player_id_for(self, entry):
        entity = (entry.get("_evidence") or {}).get("player_entity")
        if not entity:
            return None
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM players WHERE sgo_entity_id = %s LIMIT 1",
                (entity,))
            row = cursor.fetchone()
            return row[0] if row else None

    def upsert_market(self, entry, game_id, player_id):
        parsed = parse_ticker(entry["ticker"])
        game_start = None
        if parsed.family is not None:
            from market_catalog import ticker_start_timestamp
            timestamp = ticker_start_timestamp(parsed)
            if timestamp:
                game_start = datetime.fromtimestamp(timestamp, timezone.utc)
        with self.connection.cursor() as cursor:
            cursor.execute(MARKET_UPSERT, (
                entry["ticker"], game_id, player_id, parsed.prefix or None,
                parsed.prefix or None,
                getattr(parsed.family, "league", None), parsed.strike,
                parsed.side_code or None, game_start))
            self.markets_written += 1
            return cursor.fetchone()[0]

    def upsert_mapping(self, market_id, entry):
        review_state = "pending" if "_REVIEW" in entry else "approved"
        with self.connection.cursor() as cursor:
            cursor.execute(CURRENT_MAPPING, (market_id,))
            existing = cursor.fetchone()
            if existing is not None:
                if same_mapping(existing, entry):
                    self.mappings_unchanged += 1
                    return existing[0]
                cursor.execute(
                    "UPDATE sgo_mappings SET is_current = false WHERE id = %s",
                    (existing[0],))
            cursor.execute(MAPPING_INSERT, (
                market_id, entry.get("sgo_event"), entry.get("sgo_odd"),
                entry.get("sgo_line"), bool(entry.get("sgo_invert")),
                entry.get("_confidence"), review_state, entry.get("_REVIEW")))
            self.mappings_written += 1
            return cursor.fetchone()[0]

    def record(self, entry):
        game_id = self.upsert_game(entry)
        player_id = self.player_id_for(entry)
        market_id = self.upsert_market(entry, game_id, player_id)
        self.upsert_mapping(market_id, entry)
        self.connection.commit()

    def record_all(self, entries):
        for entry in entries:
            try:
                self.record(entry)
            except Exception as error:
                self.connection.rollback()
                log.warning("could not record %s: %s", entry.get("ticker"),
                            error)

    def approved_entries(self):
        with self.connection.cursor() as cursor:
            cursor.execute(APPROVED_MAPPINGS)
            entries = []
            for ticker, event_id, odd_id, line, invert in cursor.fetchall():
                entry = {"ticker": ticker, "sgo_event": event_id,
                         "sgo_odd": odd_id}
                if line is not None:
                    entry["sgo_line"] = float(line)
                if invert:
                    entry["sgo_invert"] = True
                entries.append(entry)
            return entries

    def approve(self, tickers, reviewed_by=None):
        approved = 0
        with self.connection.cursor() as cursor:
            for ticker in tickers:
                cursor.execute(
                    "UPDATE sgo_mappings SET review_state = 'approved', "
                    "reviewed_at = now(), reviewed_by = %s "
                    "FROM markets m WHERE m.id = sgo_mappings.market_id "
                    "AND m.ticker = %s AND sgo_mappings.is_current",
                    (reviewed_by or os.environ.get("USER"), ticker))
                approved += cursor.rowcount
        self.connection.commit()
        return approved

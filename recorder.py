import asyncio
import collections
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone

log = logging.getLogger("recorder")

QUEUE_LIMIT = 20000
WRITE_INTERVAL_SECONDS = 2.0
BATCH_LIMIT = 2000
MAXIMUM_RETRY_SECONDS = 30.0

BOOK_SNAPSHOT_COLUMNS = ("market_id", "ts", "best_bid_cents", "best_ask_cents",
                         "mid_cents", "microprice_cents", "spread_cents",
                         "bid_depth", "ask_depth")
QUOTE_COLUMNS = ("session_id", "market_id", "ts", "bid_cents", "ask_cents",
                 "quote_size", "inventory", "sigma_per_sqrt_second",
                 "tau_seconds", "external_fair", "blended_fair_cents",
                 "reservation_cents", "half_spread_cents")
FAIR_VALUE_COLUMNS = ("market_id", "ts", "source", "probability", "line",
                      "bookmaker_count", "age_seconds")

TABLE_COLUMNS = {"book_snapshots": BOOK_SNAPSHOT_COLUMNS,
                 "quotes": QUOTE_COLUMNS,
                 "fair_values": FAIR_VALUE_COLUMNS}

MARKET_UPSERT = """
INSERT INTO markets (ticker, family, league, strike, side_code, game_start,
                     close_time)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (ticker) DO UPDATE SET
    family = COALESCE(EXCLUDED.family, markets.family),
    league = COALESCE(EXCLUDED.league, markets.league),
    strike = COALESCE(EXCLUDED.strike, markets.strike),
    side_code = COALESCE(EXCLUDED.side_code, markets.side_code),
    game_start = COALESCE(EXCLUDED.game_start, markets.game_start),
    close_time = COALESCE(EXCLUDED.close_time, markets.close_time)
RETURNING id
"""


def current_git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True,
                              timeout=2).stdout.strip() or None
    except Exception:
        return None


def as_timestamp(moment):
    return datetime.fromtimestamp(moment, timezone.utc)


class Recorder:
    def __init__(self, environment, is_live, config=None, url=None,
                 queue_limit=QUEUE_LIMIT):
        self.environment = environment
        self.is_live = is_live
        self.config = config or {}
        self.url = url or os.environ.get("DATABASE_URL")
        self.pending = collections.deque(maxlen=queue_limit)
        self.connection = None
        self.session_id = None
        self.market_ids = {}
        self.market_details = {}
        self.dropped = 0
        self.written = 0
        self.failures = 0
        self.last_error = None
        self._stop_requested = asyncio.Event()

    @property
    def enabled(self):
        return bool(self.url)

    @property
    def connected(self):
        return self.connection is not None and self.session_id is not None

    def track_market(self, ticker, parsed=None, close_timestamp=None):
        self.market_details[ticker] = {
            "family": getattr(parsed, "prefix", None),
            "league": getattr(getattr(parsed, "family", None), "league", None),
            "strike": getattr(parsed, "strike", None),
            "side_code": getattr(parsed, "side_code", None) or None,
            "game_start": None,
            "close_time": (as_timestamp(close_timestamp)
                           if close_timestamp else None)}

    def submit(self, table, ticker, values):
        if not self.enabled:
            return
        try:
            if len(self.pending) == self.pending.maxlen:
                self.dropped += 1
            self.pending.append((table, ticker, values))
        except Exception as error:
            log.debug("recorder submit failed: %s", error)

    def record_book(self, ticker, moment, book):
        try:
            self.submit("book_snapshots", ticker, (
                as_timestamp(moment), book.best_bid_cents, book.best_ask_cents,
                book.mid_cents, book.microprice_cents, book.spread_cents,
                sum(book.yes_bids.values()) or None,
                sum(book.no_bids.values()) or None))
        except Exception as error:
            log.debug("recorder record_book failed: %s", error)

    def record_quote(self, ticker, moment, quotes, inventory, sigma,
                     tau_seconds, external_fair, blended_fair=None):
        try:
            self.submit("quotes", ticker, (
                as_timestamp(moment),
                getattr(quotes, "bid_cents", None),
                getattr(quotes, "ask_cents", None),
                getattr(quotes, "bid_size", None),
                inventory, sigma, tau_seconds, external_fair,
                blended_fair, None, None))
        except Exception as error:
            log.debug("recorder record_quote failed: %s", error)

    def record_fair_value(self, ticker, moment, watcher):
        try:
            if watcher is None or watcher.fair_probability is None:
                return
            self.submit("fair_values", ticker, (
                as_timestamp(moment), watcher.source or "unknown",
                watcher.fair_probability,
                float(watcher.consensus_line)
                if watcher.consensus_line else None,
                None, watcher.age_seconds()))
        except Exception as error:
            log.debug("recorder record_fair_value failed: %s", error)

    def open_connection(self):
        import db
        self.connection = db.connect(self.url)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO sessions (environment, is_live, git_commit, "
                "config) VALUES (%s, %s, %s, %s) RETURNING id",
                (self.environment, self.is_live, current_git_commit(),
                 json.dumps(self.config)))
            self.session_id = cursor.fetchone()[0]
        self.connection.commit()
        self.market_ids = {}

    def ensure_market(self, ticker):
        if ticker in self.market_ids:
            return self.market_ids[ticker]
        details = self.market_details.get(ticker, {})
        with self.connection.cursor() as cursor:
            cursor.execute(MARKET_UPSERT, (
                ticker, details.get("family"), details.get("league"),
                details.get("strike"), details.get("side_code"),
                details.get("game_start"), details.get("close_time")))
            self.market_ids[ticker] = cursor.fetchone()[0]
        self.connection.commit()
        return self.market_ids[ticker]

    def drain(self, limit=BATCH_LIMIT):
        batch = []
        while self.pending and len(batch) < limit:
            batch.append(self.pending.popleft())
        return batch

    def write_batch(self, batch):
        grouped = collections.defaultdict(list)
        for table, ticker, values in batch:
            market_id = self.ensure_market(ticker)
            if table == "quotes":
                grouped[table].append((self.session_id, market_id) + values)
            else:
                grouped[table].append((market_id,) + values)
        with self.connection.cursor() as cursor:
            for table, rows in grouped.items():
                columns = TABLE_COLUMNS[table]
                placeholders = ", ".join(["%s"] * len(columns))
                cursor.executemany(
                    f"INSERT INTO {table} ({', '.join(columns)}) "
                    f"VALUES ({placeholders}) ON CONFLICT DO NOTHING", rows)
        self.connection.commit()
        self.written += len(batch)

    def flush_once(self):
        if not self.connected:
            self.open_connection()
            log.info("recorder connected (session %s)", self.session_id)
        batch = self.drain()
        if batch:
            self.write_batch(batch)

    def disconnect(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
        self.connection = None
        self.session_id = None

    def close_session(self):
        if not self.connected:
            return
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE sessions SET ended_at = now() WHERE id = %s",
                    (self.session_id,))
            self.connection.commit()
        except Exception as error:
            log.debug("could not close session: %s", error)

    def stop(self):
        self._stop_requested.set()

    async def run(self):
        if not self.enabled:
            log.info("recorder disabled (no DATABASE_URL)")
            return
        retry_seconds = 1.0
        while not self._stop_requested.is_set():
            try:
                await asyncio.to_thread(self.flush_once)
                retry_seconds = 1.0
            except Exception as error:
                self.failures += 1
                self.disconnect()
                if str(error) != self.last_error:
                    log.warning("recorder write failed (%s); trading "
                                "continues, retrying", error)
                    self.last_error = str(error)
                await asyncio.sleep(min(retry_seconds, MAXIMUM_RETRY_SECONDS))
                retry_seconds *= 2
                continue
            try:
                await asyncio.wait_for(self._stop_requested.wait(),
                                       timeout=WRITE_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
        try:
            await asyncio.to_thread(self.flush_once)
            await asyncio.to_thread(self.close_session)
        except Exception as error:
            log.debug("recorder shutdown flush failed: %s", error)
        self.disconnect()
        log.info("recorder stopped: %d rows written, %d dropped, %d failures",
                 self.written, self.dropped, self.failures)

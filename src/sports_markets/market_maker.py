import argparse
import asyncio
import json
import logging
import time

from sports_markets import kalshi
from sports_markets import quoting
from sports_markets.market_data_feed import MarketDataFeed
from sports_markets.order_manager import OrderManager
from sports_markets import game_clock
from sports_markets import kalshi
from sports_markets import market_catalog
from sports_markets import quoting
from sports_markets import recorder as recorder_module
from sports_markets.market_data_feed import MarketDataFeed
from sports_markets.order_manager import OrderManager

log = logging.getLogger("market_maker")
tick_log = logging.getLogger("market_maker.tick")

CLOSE_BUFFER_SECONDS = 60
FAIR_GAP_WARNING_CENTS = 15
RECORDER_DRAIN_SECONDS = 5.0

import collections
_recent_log = collections.deque(maxlen=200)


class _LogCapture(logging.Handler):
    def emit(self, record):
        if record.name == "market_maker.tick":
            return
        _recent_log.append(self.format(record))


def recent_log_lines():
    return list(_recent_log)[-40:]


class ManagedMarket:
    def __init__(self, spec, defaults, client, dry_run, observer=None):
        merged = {**defaults, **spec}
        self.ticker = spec["ticker"]
        self.size = int(merged.get("size", 10))
        self.max_inventory = int(merged.get("max_inventory", 50))
        self.config = quoting.QuoteConfig(
            risk_aversion=float(merged.get("gamma", 0.3)),
            fill_intensity_decay=float(merged.get("k", 50.0)),
            quote_size=self.size, max_inventory=self.max_inventory)
        self.volatility = quoting.VolatilityEWMA()
        self.manager = OrderManager(client, self.ticker, self.max_inventory,
                                    self.size, dry_run=dry_run,
                                    observer=observer)
        self.sgo_event = merged.get("sgo_event")
        self.sgo_odd = merged.get("sgo_odd")
        self.sgo_line = merged.get("sgo_line")
        self.sgo_invert = bool(merged.get("sgo_invert", False))
        self.fair_watch = None
        self.close_timestamp = time.time() + 6 * 3600
        self.horizon = None
        self.fair_was_live = False
        self.last_gap_warning = 0.0
        self.past_close = False

    def build_horizon(self, use_game_clock, game_start_override=None):
        if not use_game_clock:
            return
        parsed = market_catalog.parse_ticker(self.ticker)
        if parsed.family is None:
            log.info("[%s] no known market family; horizon falls back to "
                     "Kalshi close_time", self.ticker)
            return
        if game_start_override:
            game_start = kalshi.parse_iso_timestamp(game_start_override)
            if not game_start:
                raise SystemExit(
                    f"could not parse --game-start {game_start_override!r}; "
                    f"expected ISO8601 like 2026-07-19T23:20:00Z")
            start_source = "--game-start override"
        else:
            game_start = market_catalog.ticker_start_timestamp(parsed)
            start_source = "derived from ticker"
        if not game_start:
            log.info("[%s] no start time in the ticker; horizon falls back "
                     "to Kalshi close_time", self.ticker)
            return
        try:
            self.horizon = game_clock.GameClockHorizon(parsed.family.league,
                                                       game_start)
        except KeyError:
            log.info("[%s] no pace profile for league %s; horizon falls "
                     "back to Kalshi close_time", self.ticker,
                     parsed.family.league)
            return
        now = time.time()
        estimate = self.horizon.estimate(now)
        elapsed_minutes = self.horizon.elapsed_seconds(now) / 60
        log.info("[%s] game clock: %s, started %s (%s), %.0f min elapsed, "
                 "~%.0f min to game end; Kalshi close is %.0f min out",
                 self.ticker, estimate.league,
                 time.strftime("%H:%M", time.localtime(game_start)),
                 start_source, elapsed_minutes, estimate.seconds / 60,
                 (self.close_timestamp - now) / 60)
        if (self.horizon.elapsed_seconds(now)
                > self.horizon.profile.nominal_real_seconds):
            log.warning("[%s] ticker start time implies the game should "
                        "already be over (%.0f min elapsed vs %.0f min "
                        "typical) -- if it is delayed, pass --game-start "
                        "with the real first pitch", self.ticker,
                        elapsed_minutes,
                        self.horizon.profile.nominal_real_seconds / 60)

    def seconds_to_close(self, now):
        remaining = self.close_timestamp - now
        if self.horizon is None:
            return remaining
        return min(remaining, self.horizon.seconds_remaining(now))

    def external_fair(self):
        if self.fair_watch is None:
            return None
        live = self.fair_watch.fresh_fair()
        if live is not None:
            if not self.fair_was_live:
                log.info("[%s] SGO fair live: %.1fc YES", self.ticker,
                         live * 100)
            self.fair_was_live = True
            return live
        if self.fair_was_live:
            log.warning("[%s] SGO fair STALE (age %.0fs); quoting book-only",
                        self.ticker, self.fair_watch.age_seconds())
            self.fair_was_live = False
        return None


def single_market_spec(args):
    spec = {"ticker": args.ticker, "size": args.quote_size,
            "max_inventory": args.max_inventory, "gamma": args.risk_aversion_gamma,
            "k": args.fill_intensity_decay_k}
    for key in ("sgo_event", "sgo_odd", "sgo_line", "sgo_invert"):
        value = getattr(args, key, None)
        if value:
            spec[key] = value
    return {"defaults": {"sgo_refresh (s)": args.sgo_refresh_seconds}, "markets": [spec]}


def load_markets(source, client, dry_run, observer=None):
    if isinstance(source, str):
        config = json.load(open(source))
        fallback_poll = 10.0
    elif getattr(source, "config", None):
        config = json.load(open(source.config))
        fallback_poll = source.sgo_refresh_seconds
    else:
        config = single_market_spec(source)
        fallback_poll = source.sgo_refresh_seconds
    defaults = config.get("defaults", {})
    markets = [ManagedMarket(spec, defaults, client, dry_run,
                             observer=observer)
               for spec in config["markets"]]
    if not markets:
        raise SystemExit("config has no markets")
    tickers = [market.ticker for market in markets]
    if len(set(tickers)) != len(tickers):
        raise SystemExit("duplicate tickers in config")
    return markets, float(defaults.get("sgo_poll", fallback_poll)), config


async def run(args):
    if args.env:
        kalshi.environment = args.env

    client = kalshi.KalshiClient()
    exchange = client.get_exchange_status()
    if not exchange.get("trading_active"):
        raise SystemExit(f"exchange not trading: {exchange}")

    recorder = recorder_module.Recorder(
        environment=kalshi.environment, is_live=bool(args.live), config={})

    markets, sgo_poll, resolved_config = load_markets(
        args, client, not args.live, observer=recorder)
    recorder.config = {"source": args.config or args.ticker,
                       "data interval (s)": args.data_interval_seconds, **resolved_config}
    try:
        recorder.open_connection()
    except Exception as error:
        raise SystemExit(
            f"database unreachable at startup ({error}); start PostgreSQL "
            f"or fix DATABASE_URL before trading") from error
    log.info("recording to session %s", recorder.session_id)
    market_by_ticker = {market.ticker: market for market in markets}

    for market in markets:
        details = client.get_market(market.ticker)
        market.close_timestamp = (
            kalshi.parse_iso_timestamp(details.get("close_time"))
            or market.close_timestamp)
        log.info("[%s] status %s | closes %s | size %d max_inv %d",
                 market.ticker, details.get("status"),
                 details.get("close_time"), market.size, market.max_inventory)
        market.build_horizon(not args.no_game_clock,
                             args.game_start if len(markets) == 1 else None)
        recorder.track_market(market.ticker,
                              market_catalog.parse_ticker(market.ticker),
                              market.close_timestamp)
        if args.live:
            market.manager.position = client.get_position(market.ticker)
            log.info("[%s] starting position: %+.0f", market.ticker,
                     market.manager.position)

    if args.state_file:
        logging.getLogger().addHandler(_LogCapture())
        log.info("writing dashboard state to %s (run: python dashboard.py "
                 "--state-file %s)", args.state_file, args.state_file)

    from sports_markets.sgo_fairvalue import SgoEventPoller
    pollers_by_event = {}
    for market in markets:
        if market.sgo_odd and market.sgo_event:
            poller = pollers_by_event.setdefault(
                market.sgo_event,
                SgoEventPoller(market.sgo_event, poll_seconds=sgo_poll))
            market.fair_watch = poller.watch(
                market.sgo_odd, strike_line=market.sgo_line,
                invert=market.sgo_invert)
    for poller in pollers_by_event.values():
        poller.refresh()
    for market in markets:
        watch = market.fair_watch
        if watch and watch.fair_probability is not None:
            log.info("[%s] SGO fair: %.1fc YES (%s, line %s) -- %s",
                     market.ticker, watch.fair_probability * 100, watch.source,
                     watch.consensus_line, watch.market_name)

    feed = MarketDataFeed(list(market_by_ticker), include_fills=args.live)
    feed.on_book_update.append(
        lambda book: book.mid_cents is not None
        and market_by_ticker[book.ticker].volatility.update(
            book.mid_cents / 100.0))

    def route_fill(fill):
        ticker = fill.get("market_ticker")
        market = market_by_ticker.get(ticker)
        if market is None:
            log.warning("fill for unknown market: %s", ticker)
            return
        market.manager.apply_fill(fill)
    feed.on_fill.append(route_fill)

    tasks = [asyncio.create_task(feed.run())]
    tasks += [asyncio.create_task(poller.run())
              for poller in pollers_by_event.values()]
    recorder_task = asyncio.create_task(recorder.run())

    hard_stop = (time.time() + args.duration_minutes * 60
                 if args.duration_minutes else float("inf"))
    try:
        while time.time() < hard_stop:
            await asyncio.sleep(args.data_interval_seconds)
            now = time.time()
            rows = []
            for market in markets:
                row = step_market(market, feed, now, recorder)
                if row is not None:
                    rows.append(row)
            if markets and all(market.past_close for market in markets):
                log.info("all markets past close; stopping")
                break
            if rows:
                tick_log.info(" | ".join(
                    f"{row['ticker'][-12:]} {row['mid_cents']}c "
                    f"inv{row['position']:+.0f} ${row['pnl_dollars']:+.2f}"
                    for row in rows))
            if args.state_file:
                publish_state(args.state_file, markets, rows, hard_stop, now)
    finally:
        for market in markets:
            market.manager.cancel_all()
        for poller in pollers_by_event.values():
            poller.stop()
        recorder.stop()
        try:
            await asyncio.wait_for(recorder_task,
                                   timeout=RECORDER_DRAIN_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
        feed.stop()
        for task in tasks + [recorder_task]:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        recorder.final_flush()


def step_market(market, feed, now, recorder):
    if now > market.close_timestamp - CLOSE_BUFFER_SECONDS:
        if not market.past_close:
            log.info("[%s] close buffer reached; pulling quotes",
                     market.ticker)
            market.manager.sync_quotes(None)
            market.past_close = True
        return None
    book = feed.books[market.ticker]
    if not (book.has_snapshot and book.best_bid_cents is not None
            and book.best_ask_cents is not None):
        return None

    external_fair = market.external_fair()
    if (external_fair is not None and book.mid_cents is not None
            and abs(external_fair * 100 - book.mid_cents)
            > FAIR_GAP_WARNING_CENTS and now - market.last_gap_warning > 30):
        market.last_gap_warning = now
        log.warning("[%s] external fair %.1fc vs book mid %sc -- "
                    "check oddID/side mapping", market.ticker,
                    external_fair * 100, book.mid_cents)

    seconds_to_close = market.seconds_to_close(now)
    sigma = market.volatility.sigma_per_sqrt_second()
    quotes = quoting.compute_quotes(
        book, market.manager.position, sigma, seconds_to_close,
        market.config, external_fair)
    market.manager.sync_quotes(quotes)

    recorder.record_book(market.ticker, now, book)
    recorder.record_quote(market.ticker, now, quotes,
                          market.manager.position, sigma, seconds_to_close,
                          external_fair)
    recorder.record_fair_value(market.ticker, now, market.fair_watch)

    marked_pnl = (market.manager.session_cash_dollars
                  + market.manager.position * book.mid_cents / 100.0)
    return {
        "ticker": market.ticker, "mid_cents": book.mid_cents,
        "microprice_cents": book.microprice_cents,
        "spread_cents": book.spread_cents,
        "book": {"bids": sorted(book.yes_bids.items(),
                                key=lambda level: -level[0])[:6],
                 "asks": sorted((100 - no_price, quantity)
                                for no_price, quantity
                                in book.no_bids.items())[:6]},
        "resting": {side: (list(order[1:]) if order else None)
                    for side, order in market.manager.resting.items()},
        "position": market.manager.position, "pnl_dollars": marked_pnl,
        "fair_cents": (external_fair * 100 if external_fair is not None
                       else None),
        "theo_cents": getattr(quotes, "blended_fair_cents", None),
        "reservation_cents": getattr(quotes, "reservation_cents", None),
        "fair_age_seconds": (market.fair_watch.age_seconds()
                             if market.fair_watch
                             and market.fair_watch.fresh_fair() is not None
                             else None),
        "fill_count": len(market.manager.fills)}


def publish_state(state_file, markets, rows, hard_stop, now):
    from dashboard_state import write_state
    stop_candidates = [hard_stop] + [
        market.close_timestamp - CLOSE_BUFFER_SECONDS
        for market in markets if not market.past_close]
    total_pnl = sum(row["pnl_dollars"] for row in rows)
    total_fills = sum(len(market.manager.fills) for market in markets)
    write_state(state_file, {
        "env": kalshi.environment, "live": any(
            not market.manager.dry_run for market in markets),
        "ts": now, "stop_ts": min(stop_candidates),
        "markets": rows, "total_pnl_dollars": total_pnl,
        "market_count": len(markets), "fill_count": total_fills,
        "log": recent_log_lines()})


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(
        description="Avellaneda-Stoikov market maker for one ticker or a "
                    "JSON config of markets. Dry-run by default; --live "
                    "places real post-only orders after a typed "
                    "confirmation.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("ticker", nargs="?", default=None)
    target.add_argument("--config", metavar="PATH",
                        help="markets JSON (see markets.example.json)")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--duration-minutes", type=float, default=None)
    parser.add_argument("--data-interval-seconds", type=float, default=1.0)
    parser.add_argument("--quote_size", type=int, default=10)
    parser.add_argument("--max-inventory", type=int, default=50)
    parser.add_argument("--risk-aversion-gamma", type=float, default=0.3)
    parser.add_argument("--fill-intensity-decay-k", type=float, default=50.0)
    parser.add_argument("--state-file", default=None, metavar="PATH")
    parser.add_argument("--sgo-event", default=None)
    parser.add_argument("--sgo-odd", default=None)
    parser.add_argument("--sgo-refresh-seconds", type=float, default=10.0)
    parser.add_argument("--sgo-line", default=None, metavar="STRIKE")
    parser.add_argument("--sgo-invert", action="store_true")
    parser.add_argument("--no-game-clock", action="store_true",
                        help="use Kalshi close_time as the A-S horizon "
                             "instead of the game-pace clock")
    parser.add_argument("--game-start", default=None, metavar="ISO8601",
                        help="override the game start time derived from the "
                             "ticker (use when a game is delayed; single "
                             "market only)")
    parser.add_argument("--env", choices=["prod", "demo"], default=None)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

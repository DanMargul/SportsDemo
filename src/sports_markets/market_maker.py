import argparse
import asyncio
import collections
import json
import logging
import time

import kalshi
import quoting
from market_data_feed import MarketDataFeed
from order_manager import OrderManager
from sgo_fairvalue import SgoEventPoller

log = logging.getLogger("market_maker")

CLOSE_BUFFER_SECONDS = 60
FAIR_GAP_WARNING_CENTS = 15

_recent_log = collections.deque(maxlen=200)


class _LogCapture(logging.Handler):
    def emit(self, record):
        _recent_log.append(
            time.strftime("%H:%M:%S", time.localtime(record.created))
            + f"  {record.levelname:<7} {record.getMessage()}")


def recent_log_lines():
    return list(_recent_log)


class ManagedMarket:
    def __init__(self, spec, defaults, client, dry_run):
        merged = {**defaults, **spec}
        self.ticker = spec["ticker"]
        self.size = int(merged.get("size", 10))
        self.max_inventory = int(merged.get("max_inventory", 50))
        self.config = quoting.QuoteConfig(
            risk_aversion=float(merged.get("gamma", 0.3)),
            fill_intensity_decay=float(merged.get("k", 50.0)),
            quote_size=self.size, max_inventory=self.max_inventory)
        self.volatility = quoting.EwmaVolatility()
        self.manager = OrderManager(client, self.ticker, self.max_inventory,
                                    self.size, dry_run=dry_run)
        self.sgo_event = merged.get("sgo_event")
        self.sgo_odd = merged.get("sgo_odd")
        self.sgo_line = merged.get("sgo_line")
        self.sgo_invert = bool(merged.get("sgo_invert", False))
        self.fair_watch = None
        self.close_timestamp = time.time() + 6 * 3600
        self.fair_was_live = False
        self.last_gap_warning = 0.0
        self.past_close = False

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


def load_markets(config_path, client, dry_run):
    config = json.load(open(config_path))
    defaults = config.get("defaults", {})
    markets = [ManagedMarket(spec, defaults, client, dry_run)
               for spec in config["markets"]]
    if not markets:
        raise SystemExit("config has no markets")
    tickers = [market.ticker for market in markets]
    if len(set(tickers)) != len(tickers):
        raise SystemExit("duplicate tickers in config")
    return markets, float(defaults.get("sgo_poll", 10.0))


async def run(args):
    if args.env:
        kalshi.environment = args.env

    client = kalshi.KalshiClient()
    exchange = client.get_exchange_status()
    if not exchange.get("trading_active"):
        raise SystemExit(f"exchange not trading: {exchange}")

    markets, sgo_poll = load_markets(args.config, client, not args.live)
    market_by_ticker = {market.ticker: market for market in markets}

    for market in markets:
        details = client.get_market(market.ticker)
        market.close_timestamp = (
            kalshi.parse_iso_timestamp(details.get("close_time"))
            or market.close_timestamp)
        log.info("[%s] status %s | closes %s | size %d max_inv %d",
                 market.ticker, details.get("status"),
                 details.get("close_time"), market.size, market.max_inventory)
        if args.live:
            market.manager.position = client.get_position(market.ticker)
            log.info("[%s] starting position: %+.0f", market.ticker,
                     market.manager.position)

    if args.state_file:
        from dashboard_state import write_state
        logging.getLogger().addHandler(_LogCapture())
        log.info("writing dashboard state to %s (run: python dashboard.py "
                 "--state-file %s)", args.state_file, args.state_file)

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

    hard_stop = (time.time() + args.minutes * 60
                 if args.minutes else float("inf"))
    try:
        while time.time() < hard_stop:
            await asyncio.sleep(args.interval)
            now = time.time()
            rows = []
            for market in markets:
                row = step_market(market, feed, now)
                if row is not None:
                    rows.append(row)
            if markets and all(market.past_close for market in markets):
                log.info("all markets past close; stopping")
                break
            if rows:
                log.info(" | ".join(
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
        feed.stop()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def step_market(market, feed, now):
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

    quotes = quoting.compute_quotes(
        book, market.manager.position,
        market.volatility.sigma_per_sqrt_second(),
        market.close_timestamp - now, market.config, external_fair)
    market.manager.sync_quotes(quotes)

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
        description="Avellaneda-Stoikov market maker for an arbitrary number "
                    "of Kalshi markets from one JSON config. Dry-run by "
                    "default; --live places real post-only orders after a "
                    "typed confirmation.")
    parser.add_argument("config", help="markets JSON (see markets.example.json)")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--minutes", type=float, default=None)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--state-file", default=None, metavar="PATH")
    parser.add_argument("--env", choices=["prod", "demo"], default=None)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

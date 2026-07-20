import argparse
import asyncio
import logging
import time

import kalshi
import quoting
from market_data_feed import MarketDataFeed
from order_manager import OrderManager

log = logging.getLogger("market_maker")

CLOSE_BUFFER_SECONDS = 60
FAIR_GAP_WARNING_CENTS = 15

import collections
_recent_log = collections.deque(maxlen=200)


class _LogCapture(logging.Handler):
    def emit(self, record):
        _recent_log.append(
            time.strftime("%H:%M:%S", time.localtime(record.created))
            + f"  {record.levelname:<7} {record.getMessage()}")


def recent_log_lines():
    return list(_recent_log)


async def run_market_maker(args):
    # TODO: Reduce cognitive complexity
    if args.env:
        kalshi.environment = args.env

    client = kalshi.KalshiClient()
    exchange = client.get_exchange_status()
    if not exchange.get("trading_active"):
        raise SystemExit(f"exchange not trading: {exchange}")

    market = client.get_market(args.ticker)
    close_timestamp = (kalshi.parse_iso_timestamp(market.get("close_time"))
                       or time.time() + 6 * 3600)
    log.info("[%s] status %s | closes %s", args.ticker,
             market.get("status"), market.get("close_time"))

    manager = OrderManager(client, args.ticker, args.max_inventory, args.size,
                           dry_run=not args.live)
    if args.live:
        manager.position = client.get_position(args.ticker)
        log.info("[%s] starting position: %+.0f", args.ticker,
                 manager.position)

    if args.state_file:
        from dashboard_state import write_state
        logging.getLogger().addHandler(_LogCapture())
        log.info("writing dashboard state to %s (run: python dashboard.py "
                 "--state-file %s)", args.state_file, args.state_file)

    fair_watch = None
    fair_poller = None
    if args.sgo_odd:
        from sgo_fairvalue import EventPollerSGO
        fair_poller = EventPollerSGO(args.sgo_event, poll_seconds=args.sgo_poll)
        fair_watch = fair_poller.watch(args.sgo_odd, invert=args.sgo_invert,
                                       strike_line=args.sgo_line)
        fair_poller.refresh()
        log.info("[%s] SGO fair: %.1fc YES (%s, line %s) -- %s", args.ticker,
                 fair_watch.fair_probability * 100, fair_watch.source,
                 fair_watch.consensus_line, fair_watch.market_name)

    config = quoting.QuoteConfig(risk_aversion=args.gamma,
                                 fill_intensity_decay=args.k,
                                 quote_size=args.size,
                                 max_inventory=args.max_inventory)
    volatility = quoting.VolatilityEWMA()
    feed = MarketDataFeed([args.ticker], include_fills=args.live)
    feed.on_book_update.append(
        lambda book: book.mid_cents is not None
        and volatility.update(book.mid_cents / 100.0))
    feed.on_fill.append(
        lambda fill: manager.apply_fill(fill)
        if fill.get("market_ticker") in (None, args.ticker) else None)
    feed_task = asyncio.create_task(feed.run())
    poller_task = (asyncio.create_task(fair_poller.run())
                   if fair_poller else None)

    started_at = time.time()
    hard_stop = (started_at + args.minutes * 60
                 if args.minutes else float("inf"))
    fair_was_live = False
    last_gap_warning = 0.0
    try:
        while time.time() < hard_stop:
            await asyncio.sleep(args.interval)
            now = time.time()
            if now > close_timestamp - CLOSE_BUFFER_SECONDS:
                log.info("[%s] close buffer reached; pulling quotes", args.ticker)
                manager.sync_quotes(None)
                break
            book = feed.books[args.ticker]
            if not (book.has_snapshot and book.best_bid_cents is not None
                    and book.best_ask_cents is not None):
                continue
            external_fair = None
            if fair_watch is not None:
                external_fair = fair_watch.fresh_fair()
                if external_fair is not None and not fair_was_live:
                    log.info("[%s] SGO fair live: %.1fc YES", args.ticker,
                             external_fair * 100)
                    fair_was_live = True
                elif external_fair is None and fair_was_live:
                    log.warning("[%s] SGO fair STALE (age %.0fs); quoting "
                                "book-only", args.ticker,
                                fair_watch.age_seconds())
                    fair_was_live = False
            if (external_fair is not None and book.mid_cents is not None
                    and abs(external_fair * 100 - book.mid_cents)
                    > FAIR_GAP_WARNING_CENTS
                    and now - last_gap_warning > 30):
                last_gap_warning = now
                log.warning("[%s] external fair %.1fc vs book mid %sc -- "
                            "check oddID/side mapping", args.ticker,
                            external_fair * 100, book.mid_cents)
            quotes = quoting.compute_quotes(
                book, manager.position, volatility.sigma_per_sqrt_second(),
                close_timestamp - now, config, external_fair)
            manager.sync_quotes(quotes)
            marked_pnl = (manager.session_cash_dollars
                          + manager.position * book.mid_cents / 100.0)
            log.info("[%s] book %s/%s mid %sc | inv %+.0f | pnl $%+.2f | "
                     "quotes %s", args.ticker, book.best_bid_cents,
                     book.best_ask_cents, book.mid_cents, manager.position,
                     marked_pnl, quotes)
            if args.state_file:
                write_state(args.state_file, {
                    "ticker": args.ticker, "env": kalshi.environment,
                    "live": args.live, "ts": now,
                    "stop_ts": close_timestamp - CLOSE_BUFFER_SECONDS,
                    "mid_cents": book.mid_cents,
                    "microprice_cents": book.microprice_cents,
                    "spread_cents": book.spread_cents,
                    "book": {"bids": sorted(book.yes_bids.items(),
                                            key=lambda level: -level[0])[:8],
                             "asks": sorted((100 - no_price, quantity)
                                            for no_price, quantity
                                            in book.no_bids.items())[:8]},
                    "resting": {side: (list(order[1:]) if order else None)
                                for side, order in manager.resting.items()},
                    "position": manager.position, "pnl_dollars": marked_pnl,
                    "fill_count": len(manager.fills),
                    "fair_cents": (external_fair * 100
                                   if external_fair is not None else None),
                    "fair_age_seconds": (fair_watch.age_seconds()
                                         if fair_watch
                                         and fair_watch.fresh_fair() is not None
                                         else None),
                    "log": recent_log_lines()})
    finally:
        manager.cancel_all()
        if fair_poller is not None:
            fair_poller.stop()
        feed.stop()
        feed_task.cancel()
        for task in (feed_task, poller_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(
        description="Avellaneda-Stoikov market maker for a Kalshi market.")
    parser.add_argument("ticker")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--minutes", type=float, default=None)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--size", type=int, default=10)
    parser.add_argument("--max-inventory", type=int, default=50)
    parser.add_argument("--gamma", type=float, default=0.3)
    parser.add_argument("--k", type=float, default=50.0)
    parser.add_argument("--state-file", default=None, metavar="PATH",
                        help="write dashboard state here each tick "
                             "(then run dashboard.py against the same path)")
    parser.add_argument("--sgo-event", default=None, metavar="EVENT_ID",
                        help="SportsGameOdds eventID (see sgo_fairvalue.py)")
    parser.add_argument("--sgo-odd", default=None, metavar="ODD_ID",
                        help="SGO oddID whose side settles Kalshi YES")
    parser.add_argument("--sgo-poll", type=float, default=10.0)
    parser.add_argument("--sgo-line", default=None, metavar="STRIKE",
                        help="Kalshi strike; fair computed at this exact line "
                             "via bookmaker alternate lines")
    parser.add_argument("--sgo-invert", action="store_true",
                        help="use 1-p (if needed)")
    parser.add_argument("--env", choices=["prod", "demo"], default=None)
    args = parser.parse_args()
    if bool(args.sgo_odd) != bool(args.sgo_event):
        raise SystemExit("--sgo-event and --sgo-odd "
                         "both required for SportsGameOdds integration")
    if args.env:
        kalshi.environment = args.env
    asyncio.run(run_market_maker(args))


if __name__ == "__main__":
    main()

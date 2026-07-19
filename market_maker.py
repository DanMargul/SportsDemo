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


async def run(args):
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

    config = quoting.QuoteConfig(risk_aversion=args.gamma,
                                 fill_intensity_decay=args.k,
                                 quote_size=args.size,
                                 max_inventory=args.max_inventory)
    volatility = quoting.EwmaVolatility()
    feed = MarketDataFeed([args.ticker], include_fills=args.live)
    feed.on_book_update.append(
        lambda book: book.mid_cents is not None
        and volatility.update(book.mid_cents / 100.0))
    feed.on_fill.append(
        lambda fill: manager.apply_fill(fill)
        if fill.get("market_ticker") in (None, args.ticker) else None)
    feed_task = asyncio.create_task(feed.run())

    started_at = time.time()
    hard_stop = (started_at + args.minutes * 60
                 if args.minutes else float("inf"))
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
            quotes = quoting.compute_quotes(
                book, manager.position, volatility.sigma_per_sqrt_second(),
                close_timestamp - now, config)
            manager.sync_quotes(quotes)
            marked_pnl = (manager.session_cash_dollars
                          + manager.position * book.mid_cents / 100.0)
            log.info("[%s] book %s/%s mid %sc | inv %+.0f | pnl $%+.2f | "
                     "quotes %s", args.ticker, book.best_bid_cents,
                     book.best_ask_cents, book.mid_cents, manager.position,
                     marked_pnl, quotes)
    finally:
        manager.cancel_all()
        feed.stop()
        feed_task.cancel()
        try:
            await feed_task
        except (asyncio.CancelledError, Exception):
            pass


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(
        description="Avellaneda-Stoikov market maker for one Kalshi market. "
                    "Dry-run by default; --live places real post-only "
                    "orders after a typed confirmation.")
    parser.add_argument("ticker")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--minutes", type=float, default=None)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--size", type=int, default=10)
    parser.add_argument("--max-inventory", type=int, default=50)
    parser.add_argument("--gamma", type=float, default=0.3)
    parser.add_argument("--k", type=float, default=50.0)
    parser.add_argument("--env", choices=["prod", "demo"], default=None)
    args = parser.parse_args()
    if args.env:
        kalshi.environment = args.env
    if args.live:
        typed = input(f"LIVE orders on {kalshi.environment.upper()} with real "
                      f"money. Type '{args.ticker}' to confirm: ")
        if typed.strip() != args.ticker:
            raise SystemExit("aborted")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

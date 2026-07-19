import argparse
import asyncio
import logging
import time

import kalshi
import quoting
from market_data_feed import MarketDataFeed

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

    config = quoting.QuoteConfig(risk_aversion=args.gamma,
                                 fill_intensity_decay=args.k,
                                 quote_size=args.size,
                                 max_inventory=args.max_inventory)
    volatility = quoting.EwmaVolatility()
    feed = MarketDataFeed([args.ticker])
    feed.on_book_update.append(
        lambda book: book.mid_cents is not None
        and volatility.update(book.mid_cents / 100.0))
    feed_task = asyncio.create_task(feed.run())

    started_at = time.time()
    hard_stop = (started_at + args.minutes * 60
                 if args.minutes else float("inf"))
    inventory = 0.0
    try:
        while time.time() < hard_stop:
            await asyncio.sleep(args.interval)
            now = time.time()
            if now > close_timestamp - CLOSE_BUFFER_SECONDS:
                log.info("[%s] close buffer reached; stopping", args.ticker)
                break
            book = feed.books[args.ticker]
            if not (book.has_snapshot and book.best_bid_cents is not None
                    and book.best_ask_cents is not None):
                continue
            quotes = quoting.compute_quotes(
                book, inventory, volatility.sigma_per_sqrt_second(),
                close_timestamp - now, config)
            log.info("[%s] book %s/%s mid %sc | sigma_day %.3f | quotes %s",
                     args.ticker, book.best_bid_cents, book.best_ask_cents,
                     book.mid_cents,
                     volatility.sigma_per_sqrt_second() * (86400 ** 0.5),
                     quotes)
    finally:
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
        description="Dry-run Avellaneda-Stoikov quote loop for one Kalshi "
                    "market: streams the book, computes quotes, logs them. "
                    "Places no orders.")
    parser.add_argument("ticker")
    parser.add_argument("--minutes", type=float, default=None)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--size", type=int, default=10)
    parser.add_argument("--max-inventory", type=int, default=50)
    parser.add_argument("--gamma", type=float, default=0.3)
    parser.add_argument("--k", type=float, default=50.0)
    parser.add_argument("--env", choices=["prod", "demo"], default=None)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

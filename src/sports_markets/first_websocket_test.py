import asyncio
import logging
import sys

from sports_markets.market_data_feed import MarketDataFeed


def print_ladder(book):
    if book.has_snapshot:
        print(book.ladder_str())
        print(f"mid {book.mid_cents}c  spread {book.spread_cents}c\n")


async def main():
    if len(sys.argv) < 2:
        print("usage: python first_websocket_test.py TICKER")
        return
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    feed = MarketDataFeed([sys.argv[1]])
    feed.on_book_update.append(print_ladder)

    async def stop_after(seconds):
        await asyncio.sleep(seconds)
        feed.stop()

    await asyncio.gather(feed.run(), stop_after(30))


if __name__ == "__main__":
    asyncio.run(main())

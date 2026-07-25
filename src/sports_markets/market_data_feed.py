import asyncio
import json
import logging

import websockets

from sports_markets import kalshi
from sports_markets.order_book import OrderBook

log = logging.getLogger("market_data_feed")


class MarketDataFeed:
    def __init__(self, tickers, include_fills: bool = False):
        self.tickers = list(tickers)
        self.channels = ["orderbook_delta"] + (["fill"] if include_fills else [])
        self.books = {ticker: OrderBook(ticker=ticker) for ticker in self.tickers}
        self.on_book_update = []
        self.on_fill = []
        self.sequence_by_subscription = {}
        self.stop_requested = asyncio.Event()

    def stop(self):
        self.stop_requested.set()

    async def run(self):
        reconnect_delay_seconds = 1.0
        while not self.stop_requested.is_set():
            try:
                headers = kalshi.signed_headers("GET", kalshi.websocket_sign_path)
                connect_url = kalshi.websocket_url[kalshi.environment]
                async with websockets.connect(connect_url,
                                               additional_headers=headers,
                                               max_size=2 ** 23) as connection:
                    log.info("connected to %s", connect_url)
                    reconnect_delay_seconds = 1.0
                    self.sequence_by_subscription.clear()
                    await connection.send(json.dumps({
                        "id": 1, "cmd": "subscribe",
                        "params": {"channels": self.channels,
                                   "market_tickers": self.tickers}}))
                    async for raw_message in connection:
                        if self.stop_requested.is_set():
                            return
                        self.handle_message(json.loads(raw_message))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self.stop_requested.is_set():
                    return
                log.warning("websocket error (%s); reconnecting in %.0fs",
                            error, reconnect_delay_seconds)
                await asyncio.sleep(reconnect_delay_seconds)
                reconnect_delay_seconds = min(reconnect_delay_seconds * 2, 30.0)

    def handle_message(self, envelope: dict):
        subscription_id = envelope.get("sid")
        sequence = envelope.get("seq")
        if subscription_id is not None and sequence is not None:
            previous = self.sequence_by_subscription.get(subscription_id)
            if previous is not None and sequence != previous + 1:
                log.warning("sequence gap on sid=%s (%s->%s); book may be "
                            "stale until next reconnect",
                            subscription_id, previous, sequence)
            self.sequence_by_subscription[subscription_id] = sequence

        message_type = envelope.get("type")
        message = envelope.get("msg", {})
        if message_type == "orderbook_snapshot":
            book = self.books.get(message.get("market_ticker"))
            if book:
                book.apply_snapshot(message)
                for callback in self.on_book_update:
                    callback(book)
        elif message_type == "orderbook_delta":
            book = self.books.get(message.get("market_ticker"))
            if book and book.has_snapshot:
                book.apply_delta(message)
                for callback in self.on_book_update:
                    callback(book)
        elif message_type == "fill":
            for callback in self.on_fill:
                callback(message)

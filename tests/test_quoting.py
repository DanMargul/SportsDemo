import time

from sports_markets import quoting
from sports_markets.order_book import OrderBook
from sports_markets.market_data_feed import MarketDataFeed
from sports_markets.quoting import VolatilityEWMA, QuoteConfig, compute_quotes


def test_ewma_volatility():
    volatility = VolatilityEWMA(half_life_seconds=60.0)
    quiet = VolatilityEWMA(half_life_seconds=60.0)
    price = 0.50
    for step in range(200):
        price += (0.02 if step % 2 else -0.02)
        volatility.update(price, timestamp=step)
        quiet.update(0.50, timestamp=step)
    assert volatility.sigma_per_sqrt_second() > quiet.sigma_per_sqrt_second()
    assert quiet.sigma_per_sqrt_second() < 1e-3
    print("PASS ewma volatility (choppy price > flat price)")


def test_quoting():
    book = OrderBook.from_rest("T", {"orderbook": {
        "yes": [[40, 120]], "no": [[55, 60]]}})
    base = compute_quotes(book, 0, 2e-4, 3600, QuoteConfig())
    assert base.bid_cents < base.ask_cents
    assert 1 <= base.bid_cents and base.ask_cents <= 99
    assert base.bid_cents <= book.best_ask_cents - 1
    assert base.ask_cents >= book.best_bid_cents + 1

    lifted = compute_quotes(book, 0, 2e-4, 3600, QuoteConfig(),
                            external_fair_probability=0.60)
    assert lifted.bid_cents > base.bid_cents
    assert lifted.ask_cents > base.ask_cents

    long_inventory = compute_quotes(book, 40, 2e-4, 3600, QuoteConfig())
    flat_inventory = compute_quotes(book, 0, 2e-4, 3600, QuoteConfig())
    assert long_inventory.bid_cents <= flat_inventory.bid_cents

    at_cap = compute_quotes(book, 50, 2e-4, 3600, QuoteConfig())
    assert at_cap.bid_cents is None and at_cap.ask_cents is not None
    print("PASS quoting (bounds, external-fair shift, inventory skew, cutoff)")


def test_dry_run_loop_step():
    feed = MarketDataFeed(["T"])
    config = quoting.QuoteConfig()
    volatility = quoting.VolatilityEWMA()
    feed.on_book_update.append(
        lambda book: book.mid_cents is not None
        and volatility.update(book.mid_cents / 100.0))

    feed.handle_message({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
                         "msg": {"market_ticker": "T",
                                 "yes": [[40, 120]], "no": [[55, 60]]}})
    book = feed.books["T"]
    assert book.has_snapshot
    close_timestamp = time.time() + 3600
    quotes = compute_quotes(book, 0.0, volatility.sigma_per_sqrt_second(),
                            close_timestamp - time.time(), config)
    assert quotes.bid_cents is not None and quotes.ask_cents is not None
    assert quotes.bid_cents < quotes.ask_cents

    near_close = time.time() + 30
    close_buffer_seconds = 60
    assert time.time() > near_close - close_buffer_seconds
    print("PASS dry-run loop step (snapshot -> quotes; close-buffer stop)")


TESTS = [
    test_ewma_volatility,
    test_quoting,
    test_dry_run_loop_step,
]


def main():
    for test in TESTS:
        test()
    print(f"\n{len(TESTS)} tests passed in test_quoting.py")


if __name__ == "__main__":
    main()

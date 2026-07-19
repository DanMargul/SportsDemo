import kalshi
import devig
from order_book import OrderBook
from market_data_feed import MarketDataFeed
from quoting import EwmaVolatility, QuoteConfig, compute_quotes


def test_order_book():
    book = OrderBook.from_rest("T", {"orderbook": {
        "yes": [[41, 10], [42, 13]], "no": [[45, 20], [56, 17]]}})
    assert book.best_bid_cents == 42
    assert book.best_ask_cents == 44
    assert book.spread_cents == 2
    assert book.mid_cents == 43

    fixed_point = OrderBook.from_rest("T", {"orderbook_fp": {
        "yes_dollars": [["0.42", "13"]], "no_dollars": [["0.56", "17"]]}})
    assert fixed_point.best_bid_cents == 42
    assert fixed_point.best_ask_cents == 44

    off_cent = OrderBook.from_rest("T", {"orderbook_fp": {
        "yes_dollars": [["0.42", "13"], ["0.415", "99"]],
        "no_dollars": [["0.56", "17"], ["0.005", "88"]]}})
    assert off_cent.best_bid_cents == 42
    assert off_cent.best_ask_cents == 44
    assert 41 not in off_cent.yes_bids and 42 in off_cent.yes_bids

    weighted = OrderBook.from_rest("T", {"orderbook": {
        "yes": [[40, 300]], "no": [[59, 100]]}})
    assert weighted.best_bid_cents == 40 and weighted.best_ask_cents == 41
    assert 40.5 < weighted.microprice_cents < 41
    print("PASS order book (whole-cent, fixed-point, off-cent guard, microprice)")


def test_apply_delta():
    book = OrderBook.from_rest("T", {"orderbook": {
        "yes": [[41, 10], [42, 13]], "no": [[45, 20], [56, 17]]}})
    book.apply_delta({"side": "yes", "price": 42, "delta": -13})
    assert book.best_bid_cents == 41
    book.apply_delta({"side": "yes", "price": 43, "delta": 5})
    assert book.best_bid_cents == 43 and book.yes_bids[43] == 5
    book.apply_delta({"side": "no", "price_dollars": "0.57", "delta_fp": "8.00"})
    assert book.best_ask_cents == 43
    book.apply_delta({"side": "no", "price_dollars": "0.575", "delta_fp": "9.00"})
    assert 42.5 not in book.no_bids
    print("PASS apply_delta (add, remove, fixed-point, off-cent guard)")


def test_feed_dispatch():
    feed = MarketDataFeed(["T"])
    updates = []
    feed.on_book_update.append(lambda book: updates.append(book.best_bid_cents))
    feed.handle_message({"type": "orderbook_delta", "sid": 1, "seq": 1,
                         "msg": {"market_ticker": "T", "side": "yes",
                                 "price": 40, "delta": 100}})
    assert updates == []
    feed.handle_message({"type": "orderbook_snapshot", "sid": 1, "seq": 2,
                         "msg": {"market_ticker": "T",
                                 "yes": [[40, 50]], "no": [[57, 30]]}})
    feed.handle_message({"type": "orderbook_delta", "sid": 1, "seq": 3,
                         "msg": {"market_ticker": "T", "side": "yes",
                                 "price": 41, "delta": 20}})
    assert updates == [40, 41]
    assert feed.books["T"].best_bid_cents == 41
    print("PASS feed dispatch (delta before snapshot ignored, updates fire)")


def test_ewma_volatility():
    volatility = EwmaVolatility(half_life_seconds=60.0)
    quiet = EwmaVolatility(half_life_seconds=60.0)
    price = 0.50
    for step in range(200):
        price += (0.02 if step % 2 else -0.02)
        volatility.update(price, timestamp=step)
        quiet.update(0.50, timestamp=step)
    assert volatility.sigma_per_sqrt_second() > quiet.sigma_per_sqrt_second()
    assert quiet.sigma_per_sqrt_second() < 1e-3
    print("PASS ewma volatility (choppy price > flat price)")


def test_parse_iso_timestamp():
    earlier = kalshi.parse_iso_timestamp("2026-07-18T22:00:00Z")
    later = kalshi.parse_iso_timestamp("2026-07-18T22:05:00Z")
    assert later > earlier and later - earlier == 300.0
    assert kalshi.parse_iso_timestamp(None) == 0.0
    assert kalshi.parse_iso_timestamp("garbage") == 0.0
    print("PASS parse_iso_timestamp (ordering, missing, malformed)")


def test_devig():
    for method in ("multiplicative", "power"):
        fair, overround = devig.remove_vig([-110, -110], method)
        assert all(abs(probability - 0.5) < 1e-9 for probability in fair)
        assert abs(overround - (2 * 110 / 210 - 1)) < 1e-9

    multiplicative, _ = devig.remove_vig([-450, +350], "multiplicative")
    power, _ = devig.remove_vig([-450, +350], "power")
    assert abs(sum(power) - 1) < 1e-9
    assert power[0] > multiplicative[0]

    three_way, _ = devig.remove_vig([2.45, 3.30, 3.10], "power")
    assert abs(sum(three_way) - 1) < 1e-9 and len(three_way) == 3

    assert abs(devig.implied_probability(-110) - 110 / 210) < 1e-9
    assert abs(devig.implied_probability(2.0) - 0.5) < 1e-9
    assert abs(devig.implied_probability(0.42) - 0.42) < 1e-9
    print("PASS devig (symmetric, longshot correction, 3-way, odds formats)")


def test_taking_edge():
    book = OrderBook.from_rest("T", {"orderbook": {
        "yes": [[50, 100]], "no": [[47, 100]]}})
    edges = devig.taking_edge_cents(0.56, book)
    assert edges["buy YES"] > 0 and edges["buy NO"] < 0
    flat = devig.taking_edge_cents(0.515, book)
    assert flat["buy YES"] < 0 and flat["buy NO"] < 0
    print("PASS taking edge (mispriced side positive, fair-in-book negative)")


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


def main():
    test_order_book()
    test_apply_delta()
    test_feed_dispatch()
    test_ewma_volatility()
    test_parse_iso_timestamp()
    test_devig()
    test_taking_edge()
    test_quoting()
    print("\nall offline tests passed")


if __name__ == "__main__":
    main()

from order_book import OrderBook


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


def main():
    test_order_book()
    print("\nall offline tests passed")


if __name__ == "__main__":
    main()

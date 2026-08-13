
from sports_markets.order_manager import OrderManager, fill_price_cents, fill_direction
from sports_markets.quoting import QuotePair


class FakeClient:
    def __init__(self):
        self.calls = []

    def create_order(self, **kwargs):
        self.calls.append(("create", kwargs))
        return {"order_id": f"ord{len(self.calls)}"}

    def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))


def test_order_manager():
    client = FakeClient()
    manager = OrderManager(client, "T", max_position=30,
                           max_order_contracts=20, dry_run=False)
    manager.sync_quotes(QuotePair(41, 10, 46, 10))
    created = [kwargs for op, kwargs in client.calls if op == "create"]
    assert [order["book_side"] for order in created] == ["bid", "ask"]
    assert created[0]["price_cents"] == 41 and created[1]["price_cents"] == 46

    manager.sync_quotes(QuotePair(42, 10, 45, 10))
    assert manager.resting["bid"][1] == 41 and manager.resting["ask"][1] == 46

    manager.sync_quotes(QuotePair(44, 10, 48, 10))
    assert manager.resting["bid"][1] == 44 and manager.resting["ask"][1] == 48

    manager.position = 25
    manager.sync_quotes(QuotePair(44, 10, 48, 10))
    assert manager.resting["bid"] is None and manager.resting["ask"] is not None

    manager.cancel_all()
    assert manager.resting == {"bid": None, "ask": None}
    print("PASS order manager (reprice band, veto pulls quote, cancel-all)")


def test_dry_run_places_nothing():
    client = FakeClient()
    manager = OrderManager(client, "T", 30, 20, dry_run=True)
    manager.sync_quotes(QuotePair(41, 10, 46, 10))
    assert client.calls == []
    assert manager.resting["bid"][0] == "dry"
    print("PASS dry-run (no client calls, resting tracked locally)")


def test_fill_accounting():
    manager = OrderManager(None, "T", 50, 20, dry_run=True)
    manager.apply_fill({"market_ticker": "T", "outcome_side": "yes",
                        "count_fp": "5.00", "price": "0.40"})
    manager.apply_fill({"market_ticker": "T", "outcome_side": "no",
                        "count_fp": "5.00", "yes_price_dollars": "0.45"})
    assert manager.position == 0
    assert abs(manager.session_cash_dollars - 0.25) < 1e-9

    manager.apply_fill({"market_ticker": "T", "book_side": "bid",
                        "count_fp": "2.00", "price": "0.38"})
    assert manager.position == 2
    manager.apply_fill({"market_ticker": "T", "side": "yes",
                        "action": "sell", "count": 2, "yes_price": 41})
    assert manager.position == 0
    assert len(manager.fills) == 4
    print("PASS fill accounting (outcome_side, book_side, legacy; YES cash)")


def test_fill_helpers():
    assert fill_price_cents({"price": "0.9065"}) == 91
    assert fill_price_cents({"yes_price": 42}) == 42
    assert fill_price_cents({}, fallback=37) == 37
    assert fill_direction({"outcome_side": "no"}) == ("no", -1)
    assert fill_direction({"book_side": "bid"}) == ("yes", 1)
    assert fill_direction({"side": "yes", "action": "sell"})[1] == -1
    print("PASS fill helpers (price extraction, direction vocabularies)")


def test_fill_updates_position_from_resting():
    manager = OrderManager(None, "T", 50, 20, dry_run=True)
    manager.resting["ask"] = ("dry", 46, 10)
    manager.apply_fill({"market_ticker": "T", "book_side": "ask",
                        "count_fp": "3.00"})
    assert manager.position == -3
    assert manager.fills[-1]["price_cents"] == 46
    print("PASS fill fallback price (uses resting order when absent)")


TESTS = [
    test_order_manager,
    test_dry_run_places_nothing,
    test_fill_accounting,
    test_fill_helpers,
    test_fill_updates_position_from_resting,
]


def main():
    for test in TESTS:
        test()
    print(f"\n{len(TESTS)} tests passed in test_order_manager.py")


if __name__ == "__main__":
    main()

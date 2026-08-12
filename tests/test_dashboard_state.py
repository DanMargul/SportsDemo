import os
import tempfile
import time

from sports_markets.dashboard_state import write_state, read_state
from sports_markets.order_book import OrderBook


def test_dashboard_state_roundtrip():
    book = OrderBook.from_rest("T1", {"orderbook": {
        "yes": [[42, 50]], "no": [[55, 40]]}})
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "state.json")
    assert read_state(path) is None
    snapshot = {
        "ticker": "T1", "env": "prod", "live": False, "ts": time.time(),
        "stop_ts": time.time() + 600, "mid_cents": book.mid_cents,
        "microprice_cents": book.microprice_cents,
        "spread_cents": book.spread_cents,
        "book": {"bids": [[42, 50]], "asks": [[45, 40]]},
        "resting": {"bid": [41, 3], "ask": None}, "position": 2,
        "pnl_dollars": 0.04, "fill_count": 1, "log": ["hello"]}
    write_state(path, snapshot)
    loaded = read_state(path)
    assert loaded["ticker"] == "T1" and loaded["spread_cents"] == 3
    assert loaded["resting"]["bid"] == [41, 3]
    write_state(path, {**snapshot, "position": 9})
    assert read_state(path)["position"] == 9
    assert not any(name.endswith(".tmp") for name in os.listdir(directory))
    print("PASS dashboard state (atomic write, read, overwrite, no temp leak)")


TESTS = [
    test_dashboard_state_roundtrip,
]


def main():
    for test in TESTS:
        test()
    print(f"\n{len(TESTS)} tests passed in test_dashboard_state.py")


if __name__ == "__main__":
    main()

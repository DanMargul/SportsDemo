
from sports_markets import kalshi


def test_parse_iso_timestamp():
    earlier = kalshi.parse_iso_timestamp("2026-07-18T22:00:00Z")
    later = kalshi.parse_iso_timestamp("2026-07-18T22:05:00Z")
    assert later > earlier and later - earlier == 300.0
    assert kalshi.parse_iso_timestamp(None) == 0.0
    assert kalshi.parse_iso_timestamp("garbage") == 0.0
    print("PASS parse_iso_timestamp (ordering, missing, malformed)")


def test_order_request_body():
    captured = {}
    client = kalshi.KalshiClient()
    client.request_json = (lambda method, path, params=None, body=None,
                           signed=False: captured.update(
                               method=method, path=path, body=body)
                           or {"order_id": "x"})
    client.create_order(ticker="T", book_side="ask", contracts=1,
                        price_cents=57, client_order_id="cid")
    assert captured["method"] == "POST"
    assert captured["path"] == "/portfolio/events/orders"
    assert captured["body"]["side"] == "ask"
    assert captured["body"]["price"] == "0.57"
    assert captured["body"]["count"] == "1"
    assert captured["body"]["time_in_force"] == "good_till_canceled"
    assert captured["body"]["self_trade_prevention_type"] == "taker_at_cross"
    assert captured["body"]["post_only"] is True
    client.cancel_order("abc")
    assert captured["path"] == "/portfolio/events/orders/abc"
    print("PASS order request body (V2 endpoint, YES-price both sides)")


TESTS = [
    test_parse_iso_timestamp,
    test_order_request_body,
]


def main():
    for test in TESTS:
        test()
    print(f"\n{len(TESTS)} tests passed in test_kalshi.py")


if __name__ == "__main__":
    main()

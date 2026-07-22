import json
import os
import tempfile
import time

import kalshi
import devig
import quoting
import sgo_fairvalue
from dashboard_state import write_state, read_state
from order_book import OrderBook
from market_data_feed import MarketDataFeed
from order_manager import OrderManager, fill_price_cents, fill_direction
from quoting import EwmaVolatility, QuotePair, QuoteConfig, compute_quotes


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


def test_dry_run_loop_step():
    feed = MarketDataFeed(["T"])
    config = quoting.QuoteConfig()
    volatility = quoting.EwmaVolatility()
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


class FakeClient:
    def __init__(self):
        self.calls = []

    def create_order(self, **kwargs):
        self.calls.append(("create", kwargs))
        return {"order_id": f"ord{len(self.calls)}"}

    def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))


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


def sgo_payload(odds):
    return {"success": True, "data": [{"eventID": "EV1", "odds": odds}]}


def test_sgo_fair_value():
    OVER, UNDER = "py-P1-game-ou-over", "py-P1-game-ou-under"
    captured = {}

    def fake_get(path, params):
        captured["params"] = params
        return fake_get.payload
    sgo_fairvalue.sgo_get = fake_get

    fake_get.payload = sgo_payload({
        OVER: {"oddID": OVER, "opposingOddID": UNDER, "fairOdds": "-105",
               "fairOddsAvailable": True, "fairOverUnder": "249.5"},
        UNDER: {"oddID": UNDER, "bookOdds": "-104",
                "bookOddsAvailable": True}})
    watcher = sgo_fairvalue.SgoOddWatch("EV1", OVER)
    watcher.refresh()
    assert captured["params"]["oddID"] == OVER
    assert captured["params"]["includeOpposingOdds"] == "true"
    assert abs(watcher.fair_probability
               - devig.implied_probability("-105")) < 1e-9
    assert watcher.source == "fairOdds"
    assert watcher.fresh_fair() is not None

    fake_get.payload = sgo_payload({
        OVER: {"oddID": OVER, "opposingOddID": UNDER, "fairOdds": "-117",
               "fairOddsAvailable": False, "bookOdds": "-128",
               "bookOddsAvailable": False},
        UNDER: {"oddID": UNDER, "bookOdds": "+108",
                "bookOddsAvailable": False}})
    stale = sgo_fairvalue.SgoOddWatch("EV1", OVER)
    stale.refresh()
    assert stale.fresh_fair() is None

    fake_get.payload = sgo_payload({
        OVER: {"oddID": OVER, "opposingOddID": UNDER, "bookOdds": "-120",
               "bookOddsAvailable": True},
        UNDER: {"oddID": UNDER, "bookOdds": "+100",
                "bookOddsAvailable": True}})
    fallback = sgo_fairvalue.SgoOddWatch("EV1", OVER)
    fallback.refresh()
    expected = devig.remove_vig(["-120", "+100"], "power")[0][0]
    assert abs(fallback.fair_probability - expected) < 1e-9
    assert fallback.source == "devig(bookOdds)"

    fake_get.payload = sgo_payload({
        OVER: {"oddID": OVER, "fairOdds": "-105", "fairOddsAvailable": True}})
    inverted = sgo_fairvalue.SgoOddWatch("EV1", OVER, invert=True,
                                         max_age_seconds=1)
    inverted.refresh()
    assert abs(inverted.fair_probability
               - (1 - devig.implied_probability("-105"))) < 1e-9
    inverted.updated_at -= 2
    assert inverted.fresh_fair() is None
    print("PASS SGO fair value (fairOdds, stale gating, devig fallback, "
          "invert, staleness)")


def test_sgo_strike_matching():
    OVER, UNDER = "py-P1-game-ou-over", "py-P1-game-ou-under"
    captured = {}

    def fake_get(path, params):
        captured["params"] = params
        return fake_get.payload
    sgo_fairvalue.sgo_get = fake_get

    fake_get.payload = sgo_payload({
        OVER: {"oddID": OVER, "opposingOddID": UNDER, "fairOdds": "-105",
               "fairOddsAvailable": True, "fairOverUnder": "249.5",
               "byBookmaker": {
                   "draftkings": {"odds": "-110", "overUnder": "249.5",
                                  "available": True,
                                  "altLines": [{"odds": "+180",
                                                "overUnder": "274.5",
                                                "available": True}]},
                   "fanduel": {"odds": "-112", "overUnder": "249.5",
                               "available": True,
                               "altLines": [{"odds": "+170",
                                             "overUnder": "274.5",
                                             "available": True}]},
                   "betmgm": {"odds": "-108", "overUnder": "249.5",
                              "available": True}}},
        UNDER: {"oddID": UNDER, "opposingOddID": OVER,
                "byBookmaker": {
                    "draftkings": {"odds": "-105", "overUnder": "249.5",
                                   "available": True,
                                   "altLines": [{"odds": "-230",
                                                 "overUnder": "274.5",
                                                 "available": True}]},
                    "fanduel": {"odds": "-104", "overUnder": "249.5",
                                "available": True,
                                "altLines": [{"odds": "-215",
                                              "overUnder": "274.5",
                                              "available": True}]}}}})

    at_strike = sgo_fairvalue.SgoOddWatch("EV1", OVER, strike_line="274.5")
    at_strike.refresh()
    assert captured["params"]["includeAltLines"] == "true"
    import statistics
    expected = statistics.median([
        devig.remove_vig(["+180", "-230"], "power")[0][0],
        devig.remove_vig(["+170", "-215"], "power")[0][0]])
    assert abs(at_strike.fair_probability - expected) < 1e-9
    assert at_strike.source.startswith("altLines@274.5 (2 books")

    at_consensus = sgo_fairvalue.SgoOddWatch("EV1", OVER, strike_line="249.5")
    at_consensus.refresh()
    assert at_consensus.source == "fairOdds@249.5"
    assert abs(at_consensus.fair_probability
               - devig.implied_probability("-105")) < 1e-9

    missing = sgo_fairvalue.SgoOddWatch("EV1", OVER, strike_line="300.5")
    missing.refresh()
    assert missing.fresh_fair() is None
    print("PASS SGO strike matching (alt-line median, consensus shortcut, "
          "missing strike)")


def test_sgo_shared_poll():
    OVER, UNDER = "py-P1-game-ou-over", "py-P1-game-ou-under"
    call_log = []

    def counting_get(path, params):
        call_log.append(params)
        return sgo_payload({
            OVER: {"oddID": OVER, "opposingOddID": UNDER, "fairOdds": "-110",
                   "fairOddsAvailable": True, "fairOverUnder": "249.5"},
            UNDER: {"oddID": UNDER, "opposingOddID": OVER, "fairOdds": "+120",
                    "fairOddsAvailable": True, "fairOverUnder": "249.5"}})
    sgo_fairvalue.sgo_get = counting_get

    poller = sgo_fairvalue.SgoEventPoller("EV1")
    over_watch = poller.watch(OVER)
    under_watch = poller.watch(UNDER, invert=True)
    poller.refresh()

    assert len(call_log) == 1, f"expected 1 HTTP call, got {len(call_log)}"
    assert call_log[0]["oddID"] == f"{OVER},{UNDER}"
    assert call_log[0]["includeOpposingOdds"] == "true"
    assert "includeAltLines" not in call_log[0]
    assert over_watch.fresh_fair() is not None
    assert under_watch.fresh_fair() is not None
    assert abs(under_watch.fair_probability
               - (1 - devig.implied_probability("+120"))) < 1e-9

    strike_watch = poller.watch(OVER, strike_line="249.5")
    poller.refresh()
    assert call_log[-1]["includeAltLines"] == "true"
    print("PASS SGO shared poll (one HTTP call for many watchers, alt-line "
          "opt-in)")


def test_ticker_parsing():
    from market_catalog import parse_ticker
    total = parse_ticker("KXMLBTOTAL-26JUL191920LADNYY-9")
    assert total.strike == 8.5 and total.team_codes == ("LAD", "NYY")
    assert total.game_number == 1 and total.family.has_strike

    prop = parse_ticker("KXMLBHRR-26JUL191920LADNYYG2-LADMBETTS50-2")
    assert prop.strike == 1.5, f"prop strike {prop.strike}"
    assert prop.player_code == "MBETTS50" and prop.game_number == 2

    moneyline = parse_ticker("KXMLBGAME-26JUL191920LADNYY-NYY")
    assert moneyline.side_code == "NYY" and moneyline.strike is None

    unknown = parse_ticker("KXNFLZZZ-whatever")
    assert unknown.family is None and unknown.notes
    print("PASS ticker parsing (total, player prop, moneyline, doubleheader, "
          "unknown family)")


def test_event_ranking():
    from market_catalog import parse_ticker
    from event_matcher import rank_events
    parsed = parse_ticker("KXMLBTOTAL-26JUL191920LADNYY-9")
    events = [
        {"eventID": "RIGHT", "teams": {
            "away": {"names": {"long": "Los Angeles Dodgers"}},
            "home": {"names": {"long": "New York Yankees"}}},
         "status": {"startsAt": "2026-07-19T23:20:00Z"}},
        {"eventID": "WRONGDATE", "teams": {
            "away": {"names": {"long": "Los Angeles Dodgers"}},
            "home": {"names": {"long": "New York Yankees"}}},
         "status": {"startsAt": "2026-07-25T23:20:00Z"}},
        {"eventID": "WRONGTEAMS", "teams": {
            "away": {"names": {"long": "Detroit Tigers"}},
            "home": {"names": {"long": "Los Angeles Angels"}}},
         "status": {"startsAt": "2026-07-19T23:20:00Z"}}]
    ranked = rank_events(parsed, events)
    assert ranked[0].sgo_event_id == "RIGHT"
    assert ranked[0].confidence > ranked[1].confidence > ranked[2].confidence
    assert ranked[0].confidence >= 0.85
    print("PASS event ranking (correct match ranks first, distractors below)")


def test_odd_side_inference():
    from market_catalog import parse_ticker
    from odd_matcher import match_odd
    odds = {"points-all-game-ou-over": {}, "points-home-game-ml-home": {}}

    total = match_odd(parse_ticker("KXMLBTOTAL-26JUL191920LADNYY-9"), odds)
    assert total.invert is False and total.sgo_line == 8.5

    home = match_odd(parse_ticker("KXMLBGAME-26JUL191920LADNYY-NYY"), odds)
    assert home.invert is False   # NYY is home (last code)

    away = match_odd(parse_ticker("KXMLBGAME-26JUL182207DETLAA-DET"), odds)
    assert away.invert is True    # DET is away (first code) -> invert
    assert any("VERIFY" in c for c in away.concerns)

    assert all("SETTLEMENT" in " ".join(m.concerns).upper()
               for m in (total, home, away))
    print("PASS odd side inference (over no-invert, home no-invert, away "
          "invert+verify, settlement always flagged)")


def test_scan_fetches_sgo_once():
    import argparse, io, contextlib
    import discover, kalshi
    from market_catalog import TEAM_ALIASES
    TEAM_ALIASES["mlb"].setdefault("PHI", {"philadelphia phillies", "phillies", "phi"})
    original_get_markets = kalshi.KalshiClient.get_markets
    kalshi.KalshiClient.get_markets = lambda self, **k: [
        {"ticker": "KXMLBTOTAL-26JUL211840LADPHI-9"},
        {"ticker": "KXMLBTOTAL-26JUL211840LADPHI-8"},
        {"ticker": "KXMLBGAME-26JUL211840LADPHI-PHI"}]
    calls = {"n": 0}
    def counting_sgo(path, params):
        calls["n"] += 1
        return {"data": [{"eventID": "EV",
            "teams": {"away": {"names": {"long": "Los Angeles Dodgers"}},
                      "home": {"names": {"long": "Philadelphia Phillies"}}},
            "status": {"startsAt": "2026-07-21T22:40:00Z"},
            "odds": {"points-all-game-ou-over": {},
                     "points-home-game-ml-home": {}}}]}
    discover.sgo_get = counting_sgo
    discover._crosswalk._cache = {}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            discover.scan(argparse.Namespace(series="KXMLBTOTAL", event=None,
                status="open", max=100, search=None, out=None, env=None))
        assert calls["n"] == 1, f"expected 1 SGO fetch for 3 markets, got {calls['n']}"
    finally:
        kalshi.KalshiClient.get_markets = original_get_markets
    print("PASS scan fetches SGO once for many markets (batched)")


def test_propose_rejects_bare_prefix():
    import argparse
    import discover
    result = discover.propose(argparse.Namespace(ticker="KXMLBHRR",
                                                 search=None))
    assert result is None
    print("PASS propose rejects bare series prefix (redirects to scan)")


def test_market_enumeration_pagination():
    import kalshi
    pages = [
        {"markets": [{"ticker": f"KXMLBTOTAL-A-{i}"} for i in range(200)],
         "cursor": "P2"},
        {"markets": [{"ticker": f"KXMLBTOTAL-A-{i}"} for i in range(50)],
         "cursor": None}]
    cursors = []

    client = kalshi.KalshiClient()
    def fake_request(method, path, params=None, body=None, signed=False):
        cursors.append(params.get("cursor"))
        return pages[len(cursors) - 1]
    client.request_json = fake_request
    markets = client.get_markets(series_ticker="KXMLBTOTAL", status="open")
    assert len(markets) == 250
    assert cursors == [None, "P2"]

    client2 = kalshi.KalshiClient()
    client2.request_json = lambda *a, **k: {
        "markets": [{"ticker": f"X-{i}"} for i in range(200)], "cursor": "GO"}
    assert len(client2.get_markets(series_ticker="X", max_markets=100)) == 100
    print("PASS market enumeration (cursor pagination, max cap)")


def test_enumerate_filters_families():
    import discover
    import kalshi
    client = kalshi.KalshiClient()
    client.get_markets = lambda **k: [
        {"ticker": "KXMLBTOTAL-26JUL191920LADNYY-9"},
        {"ticker": "KXMLBGAME-26JUL191920LADNYY-NYY"},
        {"ticker": "KXUNKNOWNTHING-foo"}]
    known, skipped = discover.enumerate_markets(
        client, "KXMLBTOTAL", None, "open", 500)
    assert len(known) == 2 and len(skipped) == 1
    assert skipped[0] == "KXUNKNOWNTHING-foo"
    assert known[0][1].strike == 8.5
    print("PASS enumerate filters (known families kept, unknown skipped)")


def test_player_prop_flagged():
    from market_catalog import parse_ticker
    from odd_matcher import match_odd
    prop = match_odd(parse_ticker(
        "KXMLBHRR-26JUL191920LADNYYG2-LADMBETTS50-2"), {})
    assert prop.confidence <= 0.3
    assert any("hand" in c.lower() for c in prop.concerns)
    print("PASS player prop flagged (low confidence when unresolved)")


def test_player_code_decoding():
    from player_codes import decode_player_code, name_similarity
    teams = {"MIN", "CLE", "PHI", "LAD", "CHC"}
    d = decode_player_code("PHIKSCHWARBER12", teams)
    assert d.team == "PHI" and d.first_initial == "K"
    assert d.last_name == "SCHWARBER" and d.number == "12"
    d2 = decode_player_code("CHCPCROWARMSTRONG4", teams)
    assert d2.last_name == "CROWARMSTRONG"
    assert name_similarity(d2, "Pete Crow-Armstrong") == 1.0
    d3 = decode_player_code("LADSOHTANI17", teams)
    assert name_similarity(d3, "Shohei Ohtani") == 1.0
    assert name_similarity(d3, "Mookie Betts") == 0.0
    print("PASS player code decoding (team/initial/name/number, hyphen names)")


def test_player_resolution_in_event():
    from player_crosswalk import resolve_player, entity_ids_in_event
    event_odds = {
        "batting_hits+runs+rbi-SHOHEI_OHTANI_1_MLB-game-ou-over": {
            "statEntityName": "Shohei Ohtani"},
        "batting_hits+runs+rbi-KYLE_SCHWARBER_1_MLB-game-ou-over": {
            "statEntityName": "Kyle Schwarber"},
        "points-all-game-ou-over": {}}
    stat = "batting_hits+runs+rbi"
    harvested = entity_ids_in_event(event_odds, stat)
    assert set(harvested) == {"SHOHEI_OHTANI_1_MLB", "KYLE_SCHWARBER_1_MLB"}

    entity, score, source, _ = resolve_player(
        "LADSOHTANI17", {"LAD", "PHI"}, stat, event_odds)
    assert entity == "SHOHEI_OHTANI_1_MLB" and score == 1.0
    assert source == "matched-in-event"

    cached, cscore, csource, _ = resolve_player(
        "LADSOHTANI17", {"LAD"}, stat, {},
        crosswalk={"LADSOHTANI17": "SHOHEI_OHTANI_1_MLB"})
    assert cached == "SHOHEI_OHTANI_1_MLB" and csource == "crosswalk"

    missing, mscore, msource, mconcerns = resolve_player(
        "MINBBUXTON25", {"MIN"}, stat, event_odds)
    assert missing is None and mconcerns
    print("PASS player resolution (event harvest, crosswalk cache, "
          "no-fabrication when absent)")


def main():
    test_order_book()
    test_apply_delta()
    test_feed_dispatch()
    test_ewma_volatility()
    test_parse_iso_timestamp()
    test_devig()
    test_taking_edge()
    test_quoting()
    test_dry_run_loop_step()
    test_order_request_body()
    test_order_manager()
    test_dry_run_places_nothing()
    test_fill_accounting()
    test_fill_helpers()
    test_fill_updates_position_from_resting()
    test_dashboard_state_roundtrip()
    test_ticker_parsing()
    test_event_ranking()
    test_odd_side_inference()
    test_propose_rejects_bare_prefix()
    test_scan_fetches_sgo_once()
    test_market_enumeration_pagination()
    test_enumerate_filters_families()
    test_player_prop_flagged()
    test_player_code_decoding()
    test_player_resolution_in_event()
    test_sgo_fair_value()
    test_sgo_strike_matching()
    test_sgo_shared_poll()
    print("\nall offline tests passed")


if __name__ == "__main__":
    main()

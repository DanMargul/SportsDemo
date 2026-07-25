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


def test_league_pace_profiles():
    import game_clock
    expected_hours = {"MLB": 2.7, "NFL": 3.2, "NBA": 2.25, "NHL": 2.5,
                      "EPL": 1.9}
    for league, hours in expected_hours.items():
        profile = game_clock.profile_for(league)
        actual = profile.nominal_real_seconds / 3600.0
        assert abs(actual - hours) < 0.15, f"{league}: {actual:.2f}h"
    assert game_clock.profile_for("soccer").league == "EPL"
    assert game_clock.profile_for("mlb").league == "MLB"
    assert game_clock.profile_for("WNBA").league == "NBA"
    try:
        game_clock.profile_for("kabaddi")
        assert False, "unknown league should raise"
    except KeyError:
        pass
    print("PASS league pace profiles (nominal durations, aliases, unknown)")


def test_remaining_decreases_monotonically():
    import game_clock
    previous = float("inf")
    for inning in range(1, 10):
        estimate = game_clock.estimate_remaining(
            "MLB", game_clock.innings_remaining(inning, True))
        assert estimate.seconds < previous, f"inning {inning}"
        previous = estimate.seconds

    previous = float("inf")
    for period, clock in [(1, 900), (2, 900), (3, 900), (4, 900), (4, 60)]:
        units = game_clock.clock_units_remaining(period, clock, "NFL")
        estimate = game_clock.estimate_remaining("NFL", units)
        assert estimate.seconds < previous, f"NFL Q{period} {clock}s"
        previous = estimate.seconds
    print("PASS remaining time decreases monotonically through a game")


def test_pace_calibration():
    import game_clock
    units = game_clock.innings_remaining(6, True)
    prior = game_clock.estimate_remaining("MLB", units)

    fast = game_clock.estimate_remaining("MLB", units,
                                         elapsed_real_seconds=60 * 60)
    slow = game_clock.estimate_remaining("MLB", units,
                                         elapsed_real_seconds=130 * 60)
    assert fast.seconds < prior.seconds < slow.seconds
    assert fast.pace_factor < 1.0 < slow.pace_factor
    assert "calibrated" in fast.pace_source

    # a game running exactly on the prior pace must recover factor 1.0
    profile = game_clock.profile_for("MLB")
    split = game_clock.split_at_units_remaining(profile, units)
    on_pace = split.played_weighted_units * profile.base_seconds_per_unit
    exact = game_clock.estimate_remaining("MLB", units,
                                          elapsed_real_seconds=on_pace)
    assert abs(exact.pace_factor - 1.0) < 1e-9
    assert abs(exact.seconds - prior.seconds) < 1e-6

    # too little played to calibrate: falls back to the prior
    early = game_clock.estimate_remaining(
        "MLB", game_clock.innings_remaining(1, False),
        elapsed_real_seconds=45 * 60)
    assert early.pace_factor == 1.0 and early.pace_source == "league prior"

    # absurd elapsed is clamped rather than propagated
    absurd = game_clock.estimate_remaining("MLB", units,
                                           elapsed_real_seconds=6 * 3600)
    assert absurd.pace_factor <= game_clock.MAXIMUM_PACE_FACTOR
    print("PASS pace calibration (fast/slow, exact recovery, early fallback, "
          "clamping)")


def test_calibration_weight_ramps():
    import game_clock
    weights = [game_clock.calibration_weight(f)
               for f in (0.0, 0.05, 0.2, 0.4, 0.5, 0.9)]
    assert weights[0] == 0.0 and weights[1] == 0.0
    assert 0 < weights[2] < weights[3] < 1.0
    assert weights[4] == 1.0 and weights[5] == 1.0
    print("PASS calibration weight ramps from prior-only to fully observed")


def test_breaks_and_overtime():
    import game_clock
    before_half = game_clock.estimate_remaining(
        "NFL", game_clock.clock_units_remaining(2, 900, "NFL"))
    after_half = game_clock.estimate_remaining(
        "NFL", game_clock.clock_units_remaining(3, 900, "NFL"))
    assert before_half.remaining_break_seconds == 780.0
    assert after_half.remaining_break_seconds == 0.0

    first = game_clock.estimate_remaining(
        "NHL", game_clock.clock_units_remaining(1, 1200, "NHL"))
    third = game_clock.estimate_remaining(
        "NHL", game_clock.clock_units_remaining(3, 1200, "NHL"))
    assert first.remaining_break_seconds == 2160.0
    assert third.remaining_break_seconds == 0.0

    over = game_clock.estimate_remaining("MLB", 0.0)
    assert over.seconds == over.overtime_allowance_seconds > 0
    assert game_clock.estimate_remaining(
        "MLB", 0.0, include_overtime=False).seconds == 0.0
    print("PASS scheduled breaks drop out once passed; overtime allowance")


def test_break_seconds_do_not_scale_with_pace():
    import game_clock
    units = game_clock.clock_units_remaining(2, 900, "NFL")
    profile = game_clock.profile_for("NFL")
    split = game_clock.split_at_units_remaining(profile, units)
    on_pace = split.played_weighted_units * profile.base_seconds_per_unit
    slow = game_clock.estimate_remaining("NFL", units,
                                         elapsed_real_seconds=on_pace * 1.5)
    assert slow.pace_factor > 1.0
    assert slow.remaining_break_seconds == 780.0
    print("PASS halftime stays a fixed real duration under pace calibration")


def test_elapsed_to_units_inversion():
    import game_clock
    profile = game_clock.profile_for("MLB")
    assert game_clock.units_remaining_from_elapsed("MLB", 0) == 9.0
    assert game_clock.units_remaining_from_elapsed("MLB", 10 * 3600) == 0.0
    previous = 9.0
    for minutes in (15, 45, 90, 130, 160):
        units = game_clock.units_remaining_from_elapsed("MLB", minutes * 60)
        assert units < previous
        previous = units

    # inverting the prior then re-estimating must reproduce the elapsed time
    for minutes in (20, 60, 100):
        units = game_clock.units_remaining_from_elapsed("MLB", minutes * 60)
        split = game_clock.split_at_units_remaining(profile, units)
        rebuilt = (split.played_weighted_units * profile.base_seconds_per_unit
                   + split.played_break_seconds)
        assert abs(rebuilt - minutes * 60) < 1.0, minutes

    # a league with breaks must invert through them too
    nfl = game_clock.profile_for("NFL")
    for minutes in (30, 75, 110, 160):
        units = game_clock.units_remaining_from_elapsed("NFL", minutes * 60)
        assert 0.0 <= units <= nfl.regulation_units
    print("PASS elapsed-to-units inversion (monotone, round-trips, breaks)")


def test_game_clock_horizon():
    import game_clock
    start = 1_000_000.0
    horizon = game_clock.GameClockHorizon("MLB", start)

    assert horizon.seconds_remaining(start - 1800) > \
        horizon.seconds_remaining(start)
    previous = float("inf")
    for minutes in (0, 30, 60, 90, 120, 150):
        seconds = horizon.seconds_remaining(start + minutes * 60)
        assert seconds < previous
        previous = seconds
    assert horizon.seconds_remaining(start + 10 * 3600) >= \
        horizon.minimum_seconds

    assert "inferred" in horizon.estimate(start + 3600).pace_source
    horizon.set_game_state(game_clock.innings_remaining(8, False))
    assert "calibrated" in horizon.estimate(start + 140 * 60).pace_source
    horizon.set_game_state(None)
    assert "inferred" in horizon.estimate(start + 140 * 60).pace_source
    print("PASS game clock horizon (decays, floors, live state overrides)")


def test_maker_horizon_wiring():
    import argparse
    import game_clock
    import market_maker
    close_timestamp = 4_000_000_000.0

    def make_args(ticker, **overrides):
        base = {"ticker": ticker, "game_start": None, "no_game_clock": False}
        base.update(overrides)
        return argparse.Namespace(**base)

    # league and start time come from the ticker, no flags at all
    horizon = market_maker.build_game_horizon(
        make_args("KXMLBTOTAL-26JUL191920LADNYY-9"), close_timestamp)
    assert horizon is not None and horizon.league == "MLB"
    from datetime import datetime, timezone
    started = datetime.fromtimestamp(horizon.game_start_timestamp,
                                     timezone.utc)
    assert (started.year, started.month, started.day) == (2026, 7, 19)
    assert started.hour == 23 and started.minute == 20      # 19:20 ET

    # explicit opt-out
    assert market_maker.build_game_horizon(
        make_args("KXMLBTOTAL-26JUL191920LADNYY-9", no_game_clock=True),
        close_timestamp) is None

    # unknown family falls back rather than guessing
    assert market_maker.build_game_horizon(
        make_args("KXWEIRDTHING-whatever"), close_timestamp) is None

    # override wins over the ticker, and a bad override is fatal
    overridden = market_maker.build_game_horizon(
        make_args("KXMLBTOTAL-26JUL191920LADNYY-9",
                  game_start="2026-07-20T01:00:00Z"), close_timestamp)
    assert overridden.game_start_timestamp == \
        kalshi.parse_iso_timestamp("2026-07-20T01:00:00Z")
    try:
        market_maker.build_game_horizon(
            make_args("KXMLBTOTAL-26JUL191920LADNYY-9",
                      game_start="not-a-timestamp"), close_timestamp)
        assert False, "unparseable --game-start should exit"
    except SystemExit:
        pass

    # no horizon means the old close_time behaviour, unchanged
    assert market_maker.horizon_seconds(None, close_timestamp, 1000.0) == \
        close_timestamp - 1000.0

    # with a horizon: cap regime early, game clock regime late
    start = horizon.game_start_timestamp
    early = market_maker.horizon_seconds(horizon, close_timestamp, start + 600)
    late = market_maker.horizon_seconds(horizon, close_timestamp,
                                        start + 150 * 60)
    assert late < 1800.0 < early < close_timestamp - start
    print("PASS maker horizon wiring (derived from ticker, opt-out, override, "
          "fallbacks, two regimes)")


def test_ticker_parsing():
    from market_catalog import parse_ticker
    total = parse_ticker("KXMLBTOTAL-26JUL191920LADNYY-9")
    assert total.strike == 8.5 and total.team_codes == ("LAD", "NYY")
    assert total.game_number == 1 and total.family.has_strike

    prop = parse_ticker("KXMLBHRR-26JUL191920LADNYYG2-LADMBETTS50-2")
    assert prop.strike == 1.5, f"prop strike {prop.strike}"
    assert prop.player_code == "LADMBETTS50" and prop.game_number == 2
    from player_codes import decode_player_code
    decoded = decode_player_code(prop.player_code, set(prop.team_codes))
    assert decoded.last_name == "BETTS" and decoded.number == "50"

    moneyline = parse_ticker("KXMLBGAME-26JUL191920LADNYY-NYY")
    assert moneyline.side_code == "NYY" and moneyline.strike is None

    unknown = parse_ticker("KXNFLZZZ-whatever")
    assert unknown.family is None and unknown.notes
    from market_catalog import TEAM_ALIASES
    assert len(TEAM_ALIASES["mlb"]) == 30, "expected all 30 MLB teams"

    ath = parse_ticker("KXMLBHRR-26JUL212140ATHAZ-ATHTWHITE47-4")
    assert ath.team_codes == ("ATH", "ARI")   # AZ normalizes to ARI
    d_ath = decode_player_code(ath.player_code, set(ath.team_codes))
    assert d_ath.last_name == "WHITE" and d_ath.team == "ATH"

    sea = parse_ticker("KXMLBHRR-26JUL212140SEALAA-SEAWWILSON32-3")
    assert sea.team_codes == ("SEA", "LAA")
    d_sea = decode_player_code(sea.player_code, set(sea.team_codes))
    assert d_sea.last_name == "WILSON" and d_sea.number == "32"
    print("PASS ticker parsing (total, player prop, moneyline, doubleheader, "
          "unknown family, all-30-teams, AZ/ATH/SEA)")

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

def test_propose_rejects_bare_prefix():
    import argparse
    import discover
    result = discover.propose(argparse.Namespace(ticker="KXMLBHRR",
                                                 search=None))
    assert result is None
    print("PASS propose rejects bare series prefix (redirects to scan)")

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
    discover._id_map._cache = {}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            discover.scan(argparse.Namespace(series="KXMLBTOTAL", event=None,
                status="open", max=100, search=None, out=None, env=None))
        assert calls["n"] == 1, f"expected 1 SGO fetch for 3 markets, got {calls['n']}"
    finally:
        kalshi.KalshiClient.get_markets = original_get_markets
    print("PASS scan fetches SGO once for many markets (batched)")

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
    from player_id_map import resolve_player, entity_ids_in_event
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
        id_map={"LADSOHTANI17": "SHOHEI_OHTANI_1_MLB"})
    assert cached == "SHOHEI_OHTANI_1_MLB" and csource == "id-map"

    missing, mscore, msource, mconcerns = resolve_player(
        "MINBBUXTON25", {"MIN"}, stat, event_odds)
    assert missing is None and mconcerns
    print("PASS player resolution (event harvest, ID map cache, "
          "no-fabrication when absent)")

def test_name_scoring_against_market_names():
    from player_codes import decode_player_code, name_similarity
    decoded = decode_player_code("ATHTSODERSTROM21", {"ATH", "ARI"})
    # SGO exposes the player inside a market name, not as a bare name
    for text in ["Tyler Soderstrom Hits + Runs + RBIs",
                 "Tyler Soderstrom Hits + Runs + RBIs Over/Under",
                 "TYLER_SODERSTROM_1_MLB",
                 "Tyler Soderstrom"]:
        assert name_similarity(decoded, text) == 1.0, text
    assert name_similarity(decoded, "Over/Under") == 0.0
    assert name_similarity(decoded, "Tyler White Hits + Runs + RBIs") == 0.0

    # a contradicting first initial is evidence AGAINST, not neutral
    wilson = decode_player_code("SEAWWILSON32", {"CIN", "SEA"})
    assert name_similarity(wilson, "JACOB_WILSON_1_MLB") < 0.7
    assert name_similarity(wilson, "Jacob Wilson Hits + Runs + RBIs") < 0.7
    assert name_similarity(wilson, "WILL_WILSON_1_MLB") == 1.0
    print("PASS name scoring (market names, entity IDs, wrong-initial "
          "refused)")

def test_event_local_time_and_tiebreak():
    from market_catalog import parse_ticker
    from event_matcher import rank_events
    parsed = parse_ticker("KXMLBHRR-26JUL212140ATHAZ-ATHTSODERSTROM21-2")
    assert parsed.time_hhmm == "2140"
    events = [
        {"eventID": "RIGHT",
         "teams": {"away": {"names": {"long": "Athletics"}},
                   "home": {"names": {"long": "Arizona Diamondbacks"}}},
         "status": {"startsAt": "2026-07-22T01:40:00Z"}},
        {"eventID": "NEXTDAY",
         "teams": {"away": {"names": {"long": "Athletics"}},
                   "home": {"names": {"long": "Arizona Diamondbacks"}}},
         "status": {"startsAt": "2026-07-22T22:10:00Z"}}]
    ranked = rank_events(parsed, events)
    assert ranked[0].sgo_event_id == "RIGHT"
    # a 21:40 ET game stored as next-day UTC must still be a full date match
    assert ranked[0].confidence >= 0.9, ranked[0].confidence
    assert ranked[0].confidence > ranked[1].confidence
    print("PASS event local-time date match and start-time tiebreak")

def test_hard_player_name_shapes():
    from player_codes import decode_player_code, name_similarity
    from market_catalog import parse_ticker, ticker_codes_for

    # multi-word surname: Kalshi concatenates, SGO splits
    delacruz = decode_player_code("CINEDELACRUZ44", {"CIN", "SEA"})
    assert delacruz.last_name == "DELACRUZ"
    assert name_similarity(delacruz, "ELLY_DE_LA_CRUZ_1_MLB") == 1.0

    # dropped accent: Kalshi RODRGUEZ vs SGO RODRIGUEZ
    rodriguez = decode_player_code("SEAJRODRGUEZ44", {"CIN", "SEA"})
    assert name_similarity(rodriguez, "JULIO_RODRIGUEZ_1_MLB") >= 0.85
    assert name_similarity(rodriguez, "Julio Rodr\u00edguez Hits + Runs") >= 0.85

    # Kalshi ticker-code alias in the player segment (AZ, not ARI)
    parsed = parse_ticker("KXMLBHRR-26JUL221540ATHAZ-AZCCARROLL7-1")
    codes = ticker_codes_for(parsed.league, parsed.team_codes)
    carroll = decode_player_code(parsed.player_code, codes)
    assert carroll.last_name == "CARROLL" and carroll.first_initial == "C"
    assert name_similarity(carroll, "CORBIN_CARROLL_1_MLB") == 1.0

    # fuzzy matching must not conflate genuinely different surnames
    marte = decode_player_code("AZKMARTE4", {"AZ", "ARI"})
    assert name_similarity(marte, "J_D_MARTINEZ_1_MLB") < 0.7
    print("PASS hard player names (multi-word surname, dropped accent, "
          "ticker alias, no false conflation)")

def test_ambiguous_player_forces_review():
    import argparse, io, contextlib
    import discover
    def fake_sgo(path, params):
        return {"data": [{"eventID": "EV",
            "teams": {"away": {"names": {"long": "Athletics"}},
                      "home": {"names": {"long": "Arizona Diamondbacks"}}},
            "status": {"startsAt": "2026-07-22T01:40:00Z"},
            "odds": {
                "batting_hits+runs+rbi-JACOB_WILSON_1_MLB-game-ou-over":
                    {"marketName": "Jacob Wilson Hits + Runs + RBIs"},
                "batting_hits+runs+rbi-JOSH_WILSON_1_MLB-game-ou-over":
                    {"marketName": "Josh Wilson Hits + Runs + RBIs"}}}]}
    original = discover.sgo_get
    discover.sgo_get = fake_sgo
    discover._id_map._cache = {}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            entry = discover.propose(argparse.Namespace(
                ticker="KXMLBHRR-26JUL212140ATHAZ-ATHJWILSON5-2", search=None))
    finally:
        discover.sgo_get = original
    assert "_REVIEW" in entry and "ambiguous" in entry["_REVIEW"]
    print("PASS ambiguous player match is flagged in the draft config")

def test_csv_review_roundtrip():
    import csv, io, json, os, tempfile, contextlib, argparse
    import discover

    entries = [
        {"ticker": "AAA", "sgo_event": "EV", "sgo_odd": "points-all-game-ou-over",
         "sgo_line": 8.5, "_confidence": 0.8,
         "_evidence": {"kalshi_teams": "ATH/ARI", "player_decoded": "",
                       "player_entity": "", "player_score": ""}},
        {"ticker": "BBB", "sgo_event": "EV",
         "sgo_odd": "batting_hits+runs+rbi-PLAYER_UNKNOWN-game-ou-over",
         "sgo_line": 1.5, "_confidence": 0.2, "_REVIEW": "unresolved player",
         "_evidence": {"kalshi_teams": "ATH/ARI",
                       "player_decoded": "J. Wilson #5",
                       "player_entity": "", "player_score": 0.0}},
        {"ticker": "CCC", "sgo_event": "EV", "sgo_odd": "points-home-game-ml-home",
         "sgo_invert": True, "_confidence": 0.8, "_REVIEW": "away side",
         "_evidence": {}},
    ]
    directory = tempfile.mkdtemp()
    csv_path = os.path.join(directory, "draft.csv")
    rows = discover.write_csv(entries, csv_path)
    assert rows[0]["ticker"] in {"BBB", "CCC"}      # flagged rows sort first
    assert rows[-1]["ticker"] == "AAA"
    written = list(csv.DictReader(open(csv_path)))
    assert set(discover.CSV_COLUMNS) == set(written[0].keys())

    # build skips flagged rows
    out_path = os.path.join(directory, "markets.json")
    with contextlib.redirect_stdout(io.StringIO()):
        discover.build_config(argparse.Namespace(
            csv=csv_path, out=out_path, include_flagged=False))
    config = json.load(open(out_path))
    assert [m["ticker"] for m in config["markets"]] == ["AAA"]
    assert config["markets"][0]["sgo_line"] == 8.5

    # clearing the flag admits the row, but PLAYER_UNKNOWN is still refused
    for row in written:
        row["needs_review"] = ""
    reviewed = os.path.join(directory, "reviewed.csv")
    with open(reviewed, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=discover.CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(written)
    with contextlib.redirect_stdout(io.StringIO()):
        discover.build_config(argparse.Namespace(
            csv=reviewed, out=out_path, include_flagged=False))
    config = json.load(open(out_path))
    tickers = [m["ticker"] for m in config["markets"]]
    assert "BBB" not in tickers, "PLAYER_UNKNOWN leaked into a runnable config"
    by_ticker = {m["ticker"]: m for m in config["markets"]}
    assert "CCC" in by_ticker and by_ticker["CCC"].get("sgo_invert") is True
    print("PASS CSV review roundtrip (flagged first, build skips flagged, "
          "PLAYER_UNKNOWN refused, invert preserved)")


class RecordingCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False

    def execute(self, statement, parameters=None):
        self.connection.statements.append((statement, parameters))
        if "INSERT INTO schema_migrations" in statement:
            self.connection.versions.add(parameters[0])

    def fetchall(self):
        return [(version,) for version in sorted(self.connection.versions)]


class RecordingConnection:
    def __init__(self):
        self.statements = []
        self.versions = set()
        self.commits = 0

    def cursor(self):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1


def test_migration_discovery():
    import db
    migrations = db.discover_migrations()
    assert migrations, "no migrations found"
    versions = [migration.version for migration in migrations]
    assert versions == sorted(versions), "migrations not ordered by version"
    assert len(set(versions)) == len(versions), "duplicate versions"
    assert versions[0] == 1
    for migration in migrations:
        assert migration.name and migration.name.islower()
        assert migration.sql().strip(), f"{migration.filename} is empty"
    print(f"PASS migration discovery ({len(migrations)} migrations, ordered, "
          f"uniquely versioned)")


def test_migration_filename_validation():
    import db
    import tempfile, pathlib
    directory = pathlib.Path(tempfile.mkdtemp())
    (directory / "0001_first.sql").write_text("SELECT 1;")
    (directory / "not_a_migration.sql").write_text("SELECT 1;")
    try:
        db.discover_migrations(directory)
        assert False, "badly named migration should raise"
    except ValueError:
        pass

    duplicate = pathlib.Path(tempfile.mkdtemp())
    (duplicate / "0001_first.sql").write_text("SELECT 1;")
    (duplicate / "0001_also_first.sql").write_text("SELECT 1;")
    try:
        db.discover_migrations(duplicate)
        assert False, "duplicate version should raise"
    except ValueError:
        pass

    try:
        db.discover_migrations(directory / "nonexistent")
        assert False, "missing directory should raise"
    except FileNotFoundError:
        pass
    print("PASS migration filename validation (naming, duplicates, missing)")


def test_migration_runner_applies_each_once():
    import db
    connection = RecordingConnection()
    applied = db.migrate(connection)
    assert [migration.version for migration in applied] == \
        [migration.version for migration in db.discover_migrations()]
    assert connection.commits >= len(applied)
    executed = " ".join(statement for statement, _p in connection.statements)
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in executed
    assert "CREATE TABLE markets" in executed

    again = db.migrate(connection)
    assert again == [], "second migrate should be a no-op"

    status = db.migration_status(connection)
    assert status and all(is_applied for _migration, is_applied in status)
    print("PASS migration runner (applies once, idempotent, records versions)")


def test_migration_runner_resumes_midway():
    import db
    connection = RecordingConnection()
    connection.versions.add(1)
    pending = db.pending_migrations(connection)
    assert 1 not in [migration.version for migration in pending]
    applied = db.migrate(connection)
    assert [migration.version for migration in applied] == \
        [migration.version for migration in pending]
    print("PASS migration runner resumes from a partial schema")


def test_database_url_required():
    import db
    import os
    saved = os.environ.pop("DATABASE_URL", None)
    try:
        db.database_url()
        assert False, "missing DATABASE_URL should raise"
    except RuntimeError:
        pass
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved
    os.environ["DATABASE_URL"] = "postgresql:///example"
    try:
        assert db.database_url() == "postgresql:///example"
    finally:
        os.environ.pop("DATABASE_URL", None)
        if saved is not None:
            os.environ["DATABASE_URL"] = saved
    print("PASS database url comes from the environment")


class FakePlayersCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False

    def execute(self, statement, parameters=None):
        self.connection.statements.append((statement, parameters))
        if statement.strip().upper().startswith("SELECT"):
            self.rows = [(code, entity) for code, entity
                         in sorted(self.connection.players.items())]
        elif "INSERT INTO players" in statement:
            self.connection.players[parameters[0]] = parameters[1]
            self.connection.provenance[parameters[0]] = parameters
            self.rows = []

    def fetchall(self):
        return self.rows


class FakePlayersConnection:
    def __init__(self, players=None):
        self.players = dict(players or {})
        self.provenance = {}
        self.statements = []
        self.commits = 0

    def cursor(self):
        return FakePlayersCursor(self)

    def commit(self):
        self.commits += 1


def test_json_player_id_map_roundtrip():
    import os, tempfile
    import player_id_map
    path = os.path.join(tempfile.mkdtemp(), "map.json")

    store = player_id_map.JsonPlayerIdMap(path)
    assert store.backend == "json" and len(store) == 0
    store.record("LADSOHTANI17", "SHOHEI_OHTANI_1_MLB", match_score=1.0)
    assert store["LADSOHTANI17"] == "SHOHEI_OHTANI_1_MLB"
    assert "LADSOHTANI17" in store
    assert not os.path.exists(path), "record must not write until flush"
    store.flush()

    reopened = player_id_map.JsonPlayerIdMap(path)
    assert reopened["LADSOHTANI17"] == "SHOHEI_OHTANI_1_MLB"
    reopened.record("LADSOHTANI17", "SHOHEI_OHTANI_1_MLB")
    assert reopened.dirty is False, "recording the same value is not a change"
    print("PASS json player id map (record, flush, reopen)")


def test_postgres_player_id_map_records_provenance():
    import player_id_map
    connection = FakePlayersConnection({"EXISTING1": "EXISTING_ENTITY_1_MLB"})
    store = player_id_map.PostgresPlayerIdMap(connection)
    assert store.backend == "postgres"
    assert store["EXISTING1"] == "EXISTING_ENTITY_1_MLB"

    store.record("ATHTSODERSTROM21", "TYLER_SODERSTROM_1_MLB",
                 display_name="T. Soderstrom", team_code="ATH",
                 jersey_number="21", match_score=1.0,
                 match_source="matched-in-event", source_event_id="EV_ATHAZ")
    assert store["ATHTSODERSTROM21"] == "TYLER_SODERSTROM_1_MLB"
    assert connection.commits >= 1
    recorded = connection.provenance["ATHTSODERSTROM21"]
    assert "T. Soderstrom" in recorded and "ATH" in recorded
    assert "matched-in-event" in recorded and "EV_ATHAZ" in recorded
    upsert = [statement for statement, _p in connection.statements
              if "INSERT INTO players" in statement][0]
    assert "ON CONFLICT (kalshi_code) DO UPDATE" in upsert
    print("PASS postgres player id map (loads, upserts, records provenance)")


def test_player_id_map_falls_back_without_database():
    import os, tempfile
    import player_id_map
    path = os.path.join(tempfile.mkdtemp(), "map.json")
    saved = os.environ.pop("DATABASE_URL", None)
    try:
        assert player_id_map.open_player_id_map(path=path).backend == "json"
        os.environ["DATABASE_URL"] = "postgresql://127.0.0.1:1/nothing_here"
        fallback = player_id_map.open_player_id_map(path=path)
        assert fallback.backend == "json", "unreachable database must fall back"
    finally:
        os.environ.pop("DATABASE_URL", None)
        if saved is not None:
            os.environ["DATABASE_URL"] = saved
    print("PASS player id map falls back to json without a database")


def test_player_id_map_warns_on_remap():
    import logging
    import player_id_map
    connection = FakePlayersConnection({"LADSOHTANI17": "SHOHEI_OHTANI_1_MLB"})
    store = player_id_map.PostgresPlayerIdMap(connection)
    messages = []
    handler = logging.Handler()
    handler.emit = lambda record: messages.append(record.getMessage())
    player_id_map.log.addHandler(handler)
    try:
        store.record("LADSOHTANI17", "SOMEONE_ELSE_1_MLB")
        store.record("LADSOHTANI17", "SOMEONE_ELSE_1_MLB")
    finally:
        player_id_map.log.removeHandler(handler)
    assert any("was mapped to" in message for message in messages)
    assert len([m for m in messages if "was mapped to" in m]) == 1
    print("PASS player id map warns when a code remaps to a new entity")


def test_import_json_into_postgres_is_idempotent():
    import json, os, tempfile
    import player_id_map
    path = os.path.join(tempfile.mkdtemp(), "legacy.json")
    json.dump({"A1": "ENTITY_A", "B2": "ENTITY_B"}, open(path, "w"))
    connection = FakePlayersConnection()
    imported, total = player_id_map.import_json_into_postgres(connection, path)
    assert (imported, total) == (2, 2)
    again, total_again = player_id_map.import_json_into_postgres(connection,
                                                                 path)
    assert (again, total_again) == (0, 2), "re-import must be a no-op"
    print("PASS json import into postgres is idempotent")


def test_resolve_player_accepts_store_or_dict():
    import player_id_map
    event_odds = {
        "batting_hits+runs+rbi-SHOHEI_OHTANI_1_MLB-game-ou-over":
            {"marketName": "Shohei Ohtani Hits + Runs + RBIs"}}
    connection = FakePlayersConnection({"LADSOHTANI17": "SHOHEI_OHTANI_1_MLB"})
    store = player_id_map.PostgresPlayerIdMap(connection)
    entity, score, source, _concerns = player_id_map.resolve_player(
        "LADSOHTANI17", {"LAD"}, "batting_hits+runs+rbi", {}, store)
    assert entity == "SHOHEI_OHTANI_1_MLB" and source == "id-map"

    entity, score, source, _concerns = player_id_map.resolve_player(
        "LADSOHTANI17", {"LAD"}, "batting_hits+runs+rbi", event_odds,
        {"LADSOHTANI17": "SHOHEI_OHTANI_1_MLB"})
    assert entity == "SHOHEI_OHTANI_1_MLB" and source == "id-map"
    print("PASS resolve_player works with a store or a plain dict")


def main():
    tests = [function for name, function in sorted(globals().items())
             if name.startswith("test_") and callable(function)]
    for test in tests:
        test()
    print(f"\nall offline tests passed ({len(tests)} tests)")


if __name__ == "__main__":
    main()

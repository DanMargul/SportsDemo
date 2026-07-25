import json
import os
import tempfile
import time

from sports_markets import kalshi
from sports_markets import devig
from sports_markets import quoting
from sports_markets import sgo_fairvalue
from sports_markets.dashboard_state import write_state, read_state
from sports_markets.order_book import OrderBook
from sports_markets.market_data_feed import MarketDataFeed
from sports_markets.order_manager import OrderManager, fill_price_cents, fill_direction
from sports_markets.quoting import VolatilityEWMA, QuotePair, QuoteConfig, compute_quotes


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
    test_league_pace_profiles()
    test_remaining_decreases_monotonically()
    test_pace_calibration()
    test_calibration_weight_ramps()
    test_breaks_and_overtime()
    test_break_seconds_do_not_scale_with_pace()
    test_sgo_fair_value()
    test_sgo_strike_matching()
    test_sgo_shared_poll()
    print("\nall offline tests passed")


if __name__ == "__main__":
    main()

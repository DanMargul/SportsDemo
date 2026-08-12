
from sports_markets import devig
from sports_markets import sgo_fairvalue


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

    poller.watch(OVER, strike_line="249.5")
    poller.refresh()
    assert call_log[-1]["includeAltLines"] == "true"
    print("PASS SGO shared poll (one HTTP call for many watchers, alt-line "
          "opt-in)")


TESTS = [
    test_sgo_fair_value,
    test_sgo_strike_matching,
    test_sgo_shared_poll,
]


def main():
    for test in TESTS:
        test()
    print(f"\n{len(TESTS)} tests passed in test_sgo_fairvalue.py")


if __name__ == "__main__":
    main()

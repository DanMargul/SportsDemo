


def test_league_pace_profiles():
    from sports_markets import game_clock
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
    from sports_markets import game_clock
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
    from sports_markets import game_clock
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
    from sports_markets import game_clock
    weights = [game_clock.calibration_weight(f)
               for f in (0.0, 0.05, 0.2, 0.4, 0.5, 0.9)]
    assert weights[0] == 0.0 and weights[1] == 0.0
    assert 0 < weights[2] < weights[3] < 1.0
    assert weights[4] == 1.0 and weights[5] == 1.0
    print("PASS calibration weight ramps from prior-only to fully observed")


def test_breaks_and_overtime():
    from sports_markets import game_clock
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
    from sports_markets import game_clock
    units = game_clock.clock_units_remaining(2, 900, "NFL")
    profile = game_clock.profile_for("NFL")
    split = game_clock.split_at_units_remaining(profile, units)
    on_pace = split.played_weighted_units * profile.base_seconds_per_unit
    slow = game_clock.estimate_remaining("NFL", units,
                                         elapsed_real_seconds=on_pace * 1.5)
    assert slow.pace_factor > 1.0
    assert slow.remaining_break_seconds == 780.0
    print("PASS halftime stays a fixed real duration under pace calibration")


TESTS = [
    test_league_pace_profiles,
    test_remaining_decreases_monotonically,
    test_pace_calibration,
    test_calibration_weight_ramps,
    test_breaks_and_overtime,
    test_break_seconds_do_not_scale_with_pace,
]


def main():
    for test in TESTS:
        test()
    print(f"\n{len(TESTS)} tests passed in test_game_clock.py")


if __name__ == "__main__":
    main()

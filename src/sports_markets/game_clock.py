from dataclasses import dataclass, field


@dataclass
class GameSegment:
    name: str
    units: float
    pace_multiplier: float = 1.0
    trailing_break_seconds: float = 0.0


@dataclass
class LeaguePace:
    league: str
    unit_name: str
    segments: list
    base_seconds_per_unit: float
    pace_variability: float = 0.2
    overtime_probability: float = 0.0
    overtime_expected_seconds: float = 0.0

    @property
    def regulation_units(self):
        return sum(segment.units for segment in self.segments)

    @property
    def scheduled_break_seconds(self):
        return sum(segment.trailing_break_seconds for segment in self.segments)

    @property
    def weighted_regulation_units(self):
        return sum(segment.units * segment.pace_multiplier
                   for segment in self.segments)

    @property
    def nominal_real_seconds(self):
        return (self.weighted_regulation_units * self.base_seconds_per_unit
                + self.scheduled_break_seconds)


@dataclass
class SegmentSplit:
    played_weighted_units: float
    remaining_weighted_units: float
    played_break_seconds: float
    remaining_break_seconds: float
    units_played: float
    fraction_played: float


@dataclass
class RemainingEstimate:
    league: str
    seconds: float
    low_seconds: float
    high_seconds: float
    pace_source: str
    pace_factor: float
    regulation_seconds: float
    overtime_allowance_seconds: float
    remaining_break_seconds: float
    notes: list = field(default_factory=list)

    @property
    def minutes(self):
        return self.seconds / 60.0


MINIMUM_CALIBRATION_FRACTION = 0.08
FULL_CONFIDENCE_FRACTION = 0.5
MAXIMUM_PACE_FACTOR = 2.5
MINIMUM_PACE_FACTOR = 0.4


LEAGUE_PACE_PROFILES = {
    "MLB": LeaguePace(
        league="MLB", unit_name="innings",
        segments=[GameSegment("innings 1-6", 6.0, 1.0),
                  GameSegment("innings 7-9", 3.0, 1.15)],
        base_seconds_per_unit=1016.0,
        pace_variability=0.22,
        overtime_probability=0.087, overtime_expected_seconds=1500.0),
    "NFL": LeaguePace(
        league="NFL", unit_name="clock seconds",
        segments=[GameSegment("Q1", 900.0, 1.0),
                  GameSegment("Q2", 900.0, 1.15, trailing_break_seconds=780.0),
                  GameSegment("Q3", 900.0, 1.0),
                  GameSegment("Q4", 900.0, 1.45)],
        base_seconds_per_unit=2.565,
        pace_variability=0.18,
        overtime_probability=0.062, overtime_expected_seconds=1080.0),
    "NBA": LeaguePace(
        league="NBA", unit_name="clock seconds",
        segments=[GameSegment("Q1", 720.0, 1.0),
                  GameSegment("Q2", 720.0, 1.05, trailing_break_seconds=900.0),
                  GameSegment("Q3", 720.0, 1.0),
                  GameSegment("Q4", 720.0, 1.55)],
        base_seconds_per_unit=2.174,
        pace_variability=0.18,
        overtime_probability=0.061, overtime_expected_seconds=900.0),
    "NHL": LeaguePace(
        league="NHL", unit_name="clock seconds",
        segments=[GameSegment("P1", 1200.0, 1.0, trailing_break_seconds=1080.0),
                  GameSegment("P2", 1200.0, 1.0, trailing_break_seconds=1080.0),
                  GameSegment("P3", 1200.0, 1.15)],
        base_seconds_per_unit=1.810,
        pace_variability=0.15,
        overtime_probability=0.227, overtime_expected_seconds=780.0),
    "EPL": LeaguePace(
        league="EPL", unit_name="clock seconds",
        segments=[GameSegment("H1", 2700.0, 1.05,
                              trailing_break_seconds=900.0),
                  GameSegment("H2", 2700.0, 1.12)],
        base_seconds_per_unit=1.024,
        pace_variability=0.12,
        overtime_probability=0.0, overtime_expected_seconds=0.0),
}

LEAGUE_ALIASES = {
    "BASEBALL": "MLB", "FOOTBALL": "NFL", "BASKETBALL": "NBA",
    "HOCKEY": "NHL", "SOCCER": "EPL", "FOOTBALL_SOCCER": "EPL",
    "NCAAFB": "NFL", "NCAAMB": "NBA", "WNBA": "NBA",
    "LA_LIGA": "EPL", "SERIE_A": "EPL", "BUNDESLIGA": "EPL",
    "LIGUE_1": "EPL", "MLS": "EPL", "CHAMPIONS_LEAGUE": "EPL",
}


def profile_for(league):
    key = str(league).strip().upper().replace(" ", "_")
    key = LEAGUE_ALIASES.get(key, key)
    profile = LEAGUE_PACE_PROFILES.get(key)
    if profile is None:
        raise KeyError(
            f"no pace profile for league {league!r}; known: "
            f"{sorted(LEAGUE_PACE_PROFILES)}")
    return profile


def split_at_units_remaining(profile, units_remaining):
    units_remaining = max(0.0, min(float(units_remaining),
                                   profile.regulation_units))
    units_played = profile.regulation_units - units_remaining
    consumed = 0.0
    played_weighted = remaining_weighted = 0.0
    played_breaks = remaining_breaks = 0.0
    for segment in profile.segments:
        played_here = min(max(units_played - consumed, 0.0), segment.units)
        remaining_here = segment.units - played_here
        played_weighted += played_here * segment.pace_multiplier
        remaining_weighted += remaining_here * segment.pace_multiplier
        consumed += segment.units
        if units_played >= consumed:
            played_breaks += segment.trailing_break_seconds
        else:
            remaining_breaks += segment.trailing_break_seconds
    return SegmentSplit(
        played_weighted_units=played_weighted,
        remaining_weighted_units=remaining_weighted,
        played_break_seconds=played_breaks,
        remaining_break_seconds=remaining_breaks,
        units_played=units_played,
        fraction_played=(units_played / profile.regulation_units
                         if profile.regulation_units else 0.0))


def observed_pace_factor(profile, split, elapsed_real_seconds):
    expected_play_seconds = (split.played_weighted_units
                             * profile.base_seconds_per_unit)
    if expected_play_seconds <= 0:
        return None
    observed_play_seconds = elapsed_real_seconds - split.played_break_seconds
    if observed_play_seconds <= 0:
        return None
    return observed_play_seconds / expected_play_seconds


def calibration_weight(fraction_played):
    if fraction_played < MINIMUM_CALIBRATION_FRACTION:
        return 0.0
    return min(1.0, fraction_played / FULL_CONFIDENCE_FRACTION)


def estimate_remaining(league, units_remaining, elapsed_real_seconds=None,
                       include_overtime=True):
    profile = profile_for(league)
    split = split_at_units_remaining(profile, units_remaining)
    notes = []

    pace_factor = 1.0
    pace_source = "league prior"
    if elapsed_real_seconds is not None:
        raw_factor = observed_pace_factor(profile, split, elapsed_real_seconds)
        weight = calibration_weight(split.fraction_played)
        if raw_factor is None:
            notes.append("elapsed time given but no completed play to "
                         "calibrate against")
        elif weight <= 0:
            notes.append(
                f"only {split.fraction_played:.0%} of the game played; "
                f"too little to calibrate pace")
        else:
            clamped = max(MINIMUM_PACE_FACTOR,
                          min(MAXIMUM_PACE_FACTOR, raw_factor))
            if clamped != raw_factor:
                notes.append(
                    f"observed pace factor {raw_factor:.2f} clamped to "
                    f"{clamped:.2f}")
            pace_factor = 1.0 + weight * (clamped - 1.0)
            pace_source = (f"calibrated (observed {clamped:.2f}x, "
                           f"weight {weight:.2f})")

    play_seconds = (split.remaining_weighted_units
                    * profile.base_seconds_per_unit * pace_factor)
    regulation_seconds = play_seconds + split.remaining_break_seconds

    overtime_allowance = 0.0
    if include_overtime and profile.overtime_probability > 0:
        overtime_allowance = (profile.overtime_probability
                              * profile.overtime_expected_seconds)

    seconds = regulation_seconds + overtime_allowance
    spread = profile.pace_variability * (1.0 - 0.5 * split.fraction_played)
    return RemainingEstimate(
        league=profile.league,
        seconds=seconds,
        low_seconds=max(0.0, play_seconds * (1.0 - spread)
                        + split.remaining_break_seconds),
        high_seconds=(play_seconds * (1.0 + spread)
                      + split.remaining_break_seconds
                      + (profile.overtime_expected_seconds
                         if include_overtime
                         and profile.overtime_probability > 0 else 0.0)),
        pace_source=pace_source,
        pace_factor=pace_factor,
        regulation_seconds=regulation_seconds,
        overtime_allowance_seconds=overtime_allowance,
        remaining_break_seconds=split.remaining_break_seconds,
        notes=notes)


def estimate_from_elapsed_and_remaining(league, elapsed_real_seconds,
                                        units_remaining,
                                        include_overtime=True):
    return estimate_remaining(league, units_remaining,
                              elapsed_real_seconds=elapsed_real_seconds,
                              include_overtime=include_overtime)


def innings_remaining(current_inning, is_top_half, outs=0):
    completed_half_innings = (current_inning - 1) * 2 + (0 if is_top_half else 1)
    completed_half_innings += outs / 3.0
    return max(0.0, 9.0 - completed_half_innings / 2.0)


def clock_units_remaining(period, clock_seconds_remaining, league):
    profile = profile_for(league)
    period_count = len(profile.segments)
    period = max(1, min(int(period), period_count))
    units_per_period = profile.regulation_units / period_count
    periods_after_this = period_count - period
    return max(0.0, float(clock_seconds_remaining)
               + periods_after_this * units_per_period)

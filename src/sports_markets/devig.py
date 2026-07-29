import argparse


def implied_probability(quoted_odds) -> float:
    value = float(str(quoted_odds))
    if value >= 100:
        return 1.0 / (1.0 + value / 100.0)
    if value <= -100:
        return 1.0 / (1.0 + 100.0 / abs(value))
    if value > 1.0:
        return 1.0 / value
    if 0.0 < value < 1.0:
        return value
    raise ValueError(f"cannot interpret odds: {quoted_odds!r}")


def remove_vig(quoted_odds_list, method: str = "power"):
    implied = [implied_probability(odds) for odds in quoted_odds_list]
    overround = sum(implied) - 1.0
    if method == "multiplicative" or overround <= 0.0:
        total = sum(implied)
        return [probability / total for probability in implied], overround
    exponent_low, exponent_high = 1.0, 2.0
    while sum(probability ** exponent_high for probability in implied) > 1.0:
        exponent_high *= 2
    for _ in range(200):
        exponent = (exponent_low + exponent_high) / 2
        implied_sum = sum(probability ** exponent for probability in implied)
        if implied_sum > 1.0:
            exponent_low = exponent
        else:
            exponent_high = exponent
    return [probability ** exponent for probability in implied], overround


def main():
    parser = argparse.ArgumentParser(
        description="Remove the vig from sportsbook odds. "
                    "American, decimal, and probability formats all OK.")
    parser.add_argument("odds", nargs="+")
    parser.add_argument("--method", choices=["power", "multiplicative"],
                        default="power")
    args = parser.parse_args()

    fair_probabilities, overround = remove_vig(args.odds, args.method)
    print(f"Fair Probabilities: {[float(f"{prob:.4}") for prob in fair_probabilities]}")
    print(f"Overround: {100*overround:.2f}%")

if __name__ == "__main__":
    main()

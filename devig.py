import argparse

TAKER_FEE_RATE = 0.07


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


def taking_edge_cents(fair_probability: float, book) -> dict:
    edges = {}
    if book.best_ask_cents is not None:
        ask_probability = book.best_ask_cents / 100.0
        taker_fee = TAKER_FEE_RATE * ask_probability * (1 - ask_probability)
        edges["buy YES"] = 100 * (fair_probability - ask_probability - taker_fee)
    if book.best_bid_cents is not None:
        bid_probability = book.best_bid_cents / 100.0
        taker_fee = TAKER_FEE_RATE * bid_probability * (1 - bid_probability)
        edges["buy NO"] = 100 * ((1 - fair_probability)
                                 - (1 - bid_probability) - taker_fee)
    return edges


def main():
    parser = argparse.ArgumentParser(
        description="Strip the vig from sportsbook odds -> fair probability "
                    "for Kalshi YES. Odds auto-detect American / decimal / "
                    "probability; outcome 0 maps to Kalshi YES.")
    parser.add_argument("odds", nargs="+")
    parser.add_argument("--method", choices=["power", "multiplicative"],
                        default="power")
    parser.add_argument("--outcome", type=int, default=0)
    parser.add_argument("--ticker", default=None,
                        help="compare fair vs this Kalshi market's book")
    parser.add_argument("--env", choices=["prod", "demo"], default=None)
    args = parser.parse_args()

    fair_probabilities, overround = remove_vig(args.odds, args.method)
    fair = fair_probabilities[args.outcome]
    print(f"fair probs ({args.method}): "
          + " / ".join(f"{p:.4f}" for p in fair_probabilities)
          + f"   [overround {100 * overround:.2f}%]")
    print(f"outcome {args.outcome} -> {fair * 100:.1f}c on Kalshi YES")

    if args.ticker:
        import kalshi
        from order_book import OrderBook
        if args.env:
            kalshi.environment = args.env
        client = kalshi.KalshiClient()
        book = OrderBook.from_rest(args.ticker,
                                   client.get_orderbook(args.ticker))
        print(f"{args.ticker}: bid/ask "
              f"{book.best_bid_cents}/{book.best_ask_cents}c")
        edges = taking_edge_cents(fair, book)
        for side, edge_cents in edges.items():
            print(f"  {side}: {edge_cents:+.2f}c/contract after taker fees")
        if edges and all(edge < 0 for edge in edges.values()):
            print("  no taking edge -> fair sits inside the book")


if __name__ == "__main__":
    main()

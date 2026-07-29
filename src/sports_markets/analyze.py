import argparse
import math

from sports_markets import kalshi
from sports_markets.order_book import OrderBook
from sports_markets.quoting import VolatilityEWMA


def trade_price_cents(trade):
    if trade.get("yes_price_dollars") is not None:
        dollars = float(trade["yes_price_dollars"])
        return round(dollars * 100)
    return int(trade.get("yes_price", 0))


def main():
    parser = argparse.ArgumentParser(
        description="Single-instant microstructure report for a Kalshi market.")
    parser.add_argument("ticker")
    args = parser.parse_args()

    client = kalshi.KalshiClient()
    market = client.get_market(args.ticker)
    book = OrderBook.from_rest(args.ticker, client.get_orderbook(args.ticker))

    trades = sorted(client.get_trades(args.ticker, limit=200),
                    key=lambda trade: kalshi.parse_iso_timestamp(
                        trade.get("created_time")))
    volatility = VolatilityEWMA()
    total_volume = signed_flow = 0.0
    for trade in trades:
        price_cents = trade_price_cents(trade)
        contracts = float(trade.get("count", 0))
        volatility.update(price_cents / 100.0,
                          kalshi.parse_iso_timestamp(trade.get("created_time")))
        total_volume += contracts
        signed_flow += contracts \
            if trade.get("taker_side") == "yes" \
            else -contracts

    print(f"{market.get('title', '')} | {market.get('yes_sub_title', '')}")
    print(f"status={market.get('status')} close={market.get('close_time')} "
          f"volume={market.get('volume')} oi={market.get('open_interest')}")
    print(book.ladder_str())

    best_bid, best_ask = book.best_bid_cents, book.best_ask_cents
    touch_imbalance = None
    if best_bid is not None and best_ask is not None:
        bid_quantity = book.yes_bids[best_bid]
        ask_quantity = book.no_bids[100 - best_ask]
        if bid_quantity + ask_quantity:
            touch_imbalance = ((bid_quantity - ask_quantity)
                               / (bid_quantity + ask_quantity))
    sigma_daily = volatility.sigma_per_sqrt_second() * math.sqrt(86400)

    print(f"\nmidprice:     {book.mid_cents}c    ")
    if book.microprice_cents:
          print(f"microprice:   {round(book.microprice_cents, 2)}c")
    print(f"spread       {book.spread_cents}c")
    if touch_imbalance:
        print(f"touch imbalance:    {round(touch_imbalance, 3)}")
    print(f"volatility:     {sigma_daily:.4f}/day (from last {len(trades)} trades)")


if __name__ == "__main__":
    main()

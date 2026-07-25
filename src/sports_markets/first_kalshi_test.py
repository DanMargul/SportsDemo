import sys

from sports_markets import kalshi
from sports_markets.order_book import OrderBook


def main():

    if len(sys.argv) < 2:
        print("usage: python first_kalshi_test.py TICKER")
        return
    ticker = sys.argv[1]
    client = kalshi.KalshiClient()
    print(f"exchange status: {client.get_exchange_status().get("trading_active")}")

    market = client.get_market(ticker)
    print(f"{market.get('ticker')}: {market.get('title')} | "
          f"status {market.get('status')} | closes {market.get('close_time')}")

    book = OrderBook.from_rest(ticker, client.get_orderbook(ticker))
    print(book.ladder_str())
    print(f"mid {book.mid_cents}c  microprice "
          f"{book.microprice_cents and round(book.microprice_cents, 2)}c  "
          f"spread {book.spread_cents}c")



if __name__ == "__main__":
    main()
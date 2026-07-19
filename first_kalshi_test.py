import sys

import kalshi


def main():

    start_client = kalshi.KalshiClient()
    print(f"Status: {start_client.get_exchange_status()}\n")

    if len(sys.argv) > 1:
        market_response = start_client.get_market(sys.argv[1])
        market_info = market_response.get("market", {})
        print(f"{market_info.get('ticker')}")
        print(f"Title:              {market_info.get('title')}")
        print(f"Yes Bid/Yes Ask:    ${market_info.get('yes_bid_dollars')}  / ${market_info.get('yes_ask_dollars')}")
        print(f"Closing:            {market_info.get('close_time')}")

    try:
        print(f"Balance: {start_client.get_balance()}")
    except RuntimeError as runtime_error:
        print(f"Balance Not Returned: {runtime_error}")



if __name__ == "__main__":
    main()
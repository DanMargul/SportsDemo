
# Automated Market Making Project (Work in Progress)

In the video below, a market making session is initiated on Kalshi. The market is over/under 8.5 total runs during LA Dodgers @ NY Yankees (July 19, 2026, Game 2 of doubleheader):

https://github.com/user-attachments/assets/502fc4e5-4150-45b1-831e-a4c9c4093df6

## How To Run
As demonstrated in the screen recording above, a live market making session is initiated with `python market_maker.py`

    python market_maker.py --live <KALSHI_TICKER> --sgo-event <EVENT_ID> --sgo-odd <ODD_ID> --sgo-line <LINE> --minutes <SESSION_LENGTH_MINUTES> --interval <MARKET_DATA_REFRESH_INTERVAL_SECONDS> --size <MAXIMUM_SIZE_ORDER>

The session can be visualized if `python market_maker.py` is run with the argument `--state-file <STATE_FILE_PATH>`. In a separate terminal:

    python dashboard.py --state-file <STATE_FILE_PATH>

For these commands to run without immediate failure, the user must set the following environment variables:
* KALSHI_API_KEY_ID
* KALSHI_PRIVATE_KEY_PATH
* SGO_API_KEY

## Capabilities
* Representation and analysis of (Kalshi) live order book
* Order placement (and cancellation) with Kalshi websocket
* Microstructure reporting including microprice, depth, signed flow
* Devig bookmaker odds
* Quote generation based on model of Avellaneda & Stoikov
* Sharp-book derived fair odds with SportsGameOdds poller
* Visualization of live session via dashboard

## Future
* More Documentation
* Automated discovery and matching of Kalshi 'tickers' and SportsGameOdds 'odd IDs'
* Infrastructure for trading on multiple markets simultaneously (dashboard update as well?)
* (Research) Derive inter-market correlation factors from historical data
* Trade on Polymarket (other prediction markets?)

## Most Important References
* [Kalshi API Documentation](https://docs.kalshi.com/welcome)
* [SportsGameOdds API Documentation](https://sportsgameodds.com/docs)
* [Avellaneda, M., & Stoikov, S. (2008). High-frequency trading in a limit order book. Quantitative Finance, 8(3), 217-224.](https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf)
* [Dalen, S. (2025). Toward Black Scholes for Prediction Markets: A Unified Kernel and Market Maker's Handbook. arXiv preprint arXiv:2510.15205.](https://arxiv.org/abs/2510.15205)
* [(Wikipedia) Mathematics of bookmaking -> Overround](https://en.wikipedia.org/wiki/Mathematics_of_bookmaking#Overround)

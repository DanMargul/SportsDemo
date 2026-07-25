
# Automated Market Making Project (Work in Progress)

In the video below, a market making session is initiated on Kalshi. The market is over/under 8.5 total runs during LA Dodgers @ NY Yankees (July 19, 2026, Game 2 of doubleheader):

https://github.com/user-attachments/assets/502fc4e5-4150-45b1-831e-a4c9c4093df6

## Installation

    pip install -e .

## How To Run 

This is a minimal example of a live market-making session:

    sports-marketmaker <KALSHI_TICKER> --live 

Note: Run without `--live` to prevent placing orders. Whether or not real orders will be placed, the user must have a Kalshi account with an API key and a private key file. They must be declared as environment variables:

    export KALSHI_API_KEY_ID=<KALSHI_API_KEY_ID>
    export KALSHI_PRIVATE_KEY_PATH=<KALSHI_PRIVATE_KEY_PATH>

As demonstrated in the screen recording above, a live market making session can be initiated with many more arguments:

    sports-marketmaker --live <KALSHI_TICKER> --sgo-event <EVENT_ID> --sgo-odd <ODD_ID> --sgo-line <LINE> --duration-minutes <SESSION_LENGTH_MINUTES> --data-interval-seconds <MARKET_DATA_REFRESH_INTERVAL_SECONDS> --quote-size <MAXIMUM_SIZE_ORDER>

If any of {`--sgo-event`, `--sgo-odd`, `--sgo-line`} are used, a SportsGameOdds API key is required:

    export SGO_API_KEY=<SGO_API_KEY>

The session can be visualized if `python market_maker.py` is run with the argument `--state-file <STATE_FILE_PATH>`. In a separate terminal:

    sports-dashboard --state-file <STATE_FILE_PATH>


## Capabilities
* Representation and analysis of (Kalshi) live order book
* Order placement (and cancellation) with Kalshi websocket
* Microstructure reporting including microprice, depth, signed flow
* Devig bookmaker odds
* Quote generation based on model of Avellaneda & Stoikov
* Sharp-book derived fair odds with SportsGameOdds poller
* Visualization of live session via dashboard
* Automated discovery and matching of Kalshi 'tickers' and SportsGameOdds 'odd IDs'

## Extensions Currently in Progress
* ETL with Postgresql ([Pull Request](https://github.com/DanMargul/SportsDemo/pull/3))
* Infrastructure for trading on multiple markets simultaneously (dashboard update as well?)
* Improve estimate of time-to-close; Kalshi markets for events can close days after end of game.

## Future
* More Documentation
* (Research) Derive inter-market correlation factors from historical data
* Trade on Polymarket (other prediction markets?)

## Most Important References
* [Kalshi API Documentation](https://docs.kalshi.com/welcome)
* [SportsGameOdds API Documentation](https://sportsgameodds.com/docs)
* [Avellaneda, M., & Stoikov, S. (2008). High-frequency trading in a limit order book. Quantitative Finance, 8(3), 217-224.](https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf)
* [Dalen, S. (2025). Toward Black Scholes for Prediction Markets: A Unified Kernel and Market Maker's Handbook. arXiv preprint arXiv:2510.15205.](https://arxiv.org/abs/2510.15205)
* [(Wikipedia) Mathematics of bookmaking -> Overround](https://en.wikipedia.org/wiki/Mathematics_of_bookmaking#Overround)


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

As demonstrated in the screen recording above, a live market making session can be initiated with many more arguments. The main reason for this is to use sharp book odds from SportsGameOdds to derive fair values:

    sports-marketmaker --live <KALSHI_TICKER> --sgo-event <EVENT_ID> --sgo-odd <ODD_ID>--sgo-line <LINE> 

If any of {`--sgo-event`, `--sgo-odd`, `--sgo-line`} are used, a SportsGameOdds API key is required:

    export SGO_API_KEY=<SGO_API_KEY>

## Other Capabilities

### Dashboard 

A session can be visualized if `sports-marketmaker` is run with the argument `--state-file <STATE_FILE_PATH>`. In a separate terminal:

    sports-dashboard --state-file <STATE_FILE_PATH>

### Kalshi Ticker & SportsGameOdds Event ID and Odd ID

Every market on Kalshi has a unique identifier that is a mandatory argument for `sports-marketmaker`. When viewing a Kalshi market in a web browser, the URL will end with `op_market_ticker=<KALSHI_TICKER>`.

With the Kalshi ticker, the SportsGameOdds Event ID and Odd ID can be found with:

    sports-discover propose <KALSHI_TICKER>

### Devig Bookmaker Odds

Fair prices can be computed during market-making sessions with a consensus of sharp book odds available through SportsGameOdds. These odds must have vig removed, and this functionality is also available as a command-line tool:

    sports-devig <number-1> <number-2> ... <number-N>

Examples with three different odds formats:

    sports-devig 120 -140
<img width="460" height="52" alt="image" src="https://github.com/user-attachments/assets/827e479b-d68c-4b7d-8337-faac650381fa" />

    sports-devig 0.6 0.45
<img width="460" height="52" alt="image" src="https://github.com/user-attachments/assets/845136f9-99eb-4913-b48c-0ae9999b6db5" />
  
    sports-devig 1.97 1.95
<img width="460" height="52" alt="image" src="https://github.com/user-attachments/assets/214b614d-2940-4168-b225-0a7780593841" />


  
### Snapshot of a Live Order Book

For some quick information about a live Kalshi market order book:

    sports-analyze <KALSHI_TICKER>

Example: 

    sports-analyze KXMLBGAME-26JUL261335TORBOS-BOS
    
<img width="893" height="554" alt="image" src="https://github.com/user-attachments/assets/47847678-47a3-4f44-b48e-eee7f0464f3a" />

    

## Less-Documented Capabilities
* Order placement (and cancellation) with Kalshi websocket
* Quote generation based on model of Avellaneda & Stoikov
* Automated discovery and matching of Kalshi 'tickers' and SportsGameOdds 'odd IDs'

## Extensions Currently in Progress
* ETL with Postgresql ([Pull Request](https://github.com/DanMargul/SportsDemo/pull/3))
* Infrastructure for trading on multiple markets simultaneously (dashboard update as well?)
* ~~Improve estimate of time-to-close; Kalshi markets for events can close days after end of game.~~ ([Merged](https://github.com/DanMargul/SportsDemo/pull/4))

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

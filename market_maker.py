import argparse
import asyncio
import logging
import time

import game_clock
import kalshi
import market_catalog
import recorder as recorder_module
import quoting
from market_data_feed import MarketDataFeed
from order_manager import OrderManager

log = logging.getLogger("market_maker")

CLOSE_BUFFER_SECONDS = 60
FAIR_GAP_WARNING_CENTS = 15
RECORDER_DRAIN_SECONDS = 5.0

import collections
_recent_log = collections.deque(maxlen=200)


class _LogCapture(logging.Handler):
    def emit(self, record):
        _recent_log.append(
            time.strftime("%H:%M:%S", time.localtime(record.created))
            + f"  {record.levelname:<7} {record.getMessage()}")


def recent_log_lines():
    return list(_recent_log)


async def run(args):
    if args.env:
        kalshi.environment = args.env

    client = kalshi.KalshiClient()
    exchange = client.get_exchange_status()
    if not exchange.get("trading_active"):
        raise SystemExit(f"exchange not trading: {exchange}")

    market = client.get_market(args.ticker)
    close_timestamp = (kalshi.parse_iso_timestamp(market.get("close_time"))
                       or time.time() + 6 * 3600)
    log.info("[%s] status %s | closes %s", args.ticker,
             market.get("status"), market.get("close_time"))

    horizon = build_game_horizon(args, close_timestamp)

    recorder = recorder_module.Recorder(
        environment=kalshi.environment, is_live=bool(args.live),
        config={"ticker": args.ticker, "size": args.size,
                "max_inventory": args.max_inventory, "gamma": args.gamma,
                "k": args.k, "interval": args.interval},
        url=None if not args.no_record else "")
    recorder.track_market(args.ticker, market_catalog.parse_ticker(args.ticker),
                          close_timestamp)

    manager = OrderManager(client, args.ticker, args.max_inventory, args.size,
                           dry_run=not args.live)
    if args.live:
        manager.position = client.get_position(args.ticker)
        log.info("[%s] starting position: %+.0f", args.ticker,
                 manager.position)

    if args.state_file:
        from dashboard_state import write_state
        logging.getLogger().addHandler(_LogCapture())
        log.info("writing dashboard state to %s (run: python dashboard.py "
                 "--state-file %s)", args.state_file, args.state_file)

    fair_watch = None
    fair_poller = None
    if args.sgo_odd:
        from sgo_fairvalue import SgoEventPoller
        fair_poller = SgoEventPoller(args.sgo_event, poll_seconds=args.sgo_poll)
        fair_watch = fair_poller.watch(args.sgo_odd, invert=args.sgo_invert,
                                       strike_line=args.sgo_line)
        fair_poller.refresh()
        log.info("[%s] SGO fair: %.1fc YES (%s, line %s) -- %s", args.ticker,
                 fair_watch.fair_probability * 100, fair_watch.source,
                 fair_watch.consensus_line, fair_watch.market_name)

    config = quoting.QuoteConfig(risk_aversion=args.gamma,
                                 fill_intensity_decay=args.k,
                                 quote_size=args.size,
                                 max_inventory=args.max_inventory)
    volatility = quoting.EwmaVolatility()
    feed = MarketDataFeed([args.ticker], include_fills=args.live)
    feed.on_book_update.append(
        lambda book: book.mid_cents is not None
        and volatility.update(book.mid_cents / 100.0))
    feed.on_fill.append(
        lambda fill: manager.apply_fill(fill)
        if fill.get("market_ticker") in (None, args.ticker) else None)
    feed_task = asyncio.create_task(feed.run())
    poller_task = (asyncio.create_task(fair_poller.run())
                   if fair_poller else None)
    recorder_task = asyncio.create_task(recorder.run())

    started_at = time.time()
    hard_stop = (started_at + args.minutes * 60
                 if args.minutes else float("inf"))
    fair_was_live = False
    last_gap_warning = 0.0
    try:
        while time.time() < hard_stop:
            await asyncio.sleep(args.interval)
            now = time.time()
            if now > close_timestamp - CLOSE_BUFFER_SECONDS:
                log.info("[%s] close buffer reached; pulling quotes", args.ticker)
                manager.sync_quotes(None)
                break
            book = feed.books[args.ticker]
            if not (book.has_snapshot and book.best_bid_cents is not None
                    and book.best_ask_cents is not None):
                continue
            external_fair = None
            if fair_watch is not None:
                external_fair = fair_watch.fresh_fair()
                if external_fair is not None and not fair_was_live:
                    log.info("[%s] SGO fair live: %.1fc YES", args.ticker,
                             external_fair * 100)
                    fair_was_live = True
                elif external_fair is None and fair_was_live:
                    log.warning("[%s] SGO fair STALE (age %.0fs); quoting "
                                "book-only", args.ticker,
                                fair_watch.age_seconds())
                    fair_was_live = False
            if (external_fair is not None and book.mid_cents is not None
                    and abs(external_fair * 100 - book.mid_cents)
                    > FAIR_GAP_WARNING_CENTS
                    and now - last_gap_warning > 30):
                last_gap_warning = now
                log.warning("[%s] external fair %.1fc vs book mid %sc -- "
                            "check oddID/side mapping", args.ticker,
                            external_fair * 100, book.mid_cents)
            seconds_to_close = horizon_seconds(horizon, close_timestamp, now)
            sigma = volatility.sigma_per_sqrt_second()
            quotes = quoting.compute_quotes(
                book, manager.position, sigma, seconds_to_close, config,
                external_fair)
            manager.sync_quotes(quotes)

            recorder.record_book(args.ticker, now, book)
            recorder.record_quote(args.ticker, now, quotes, manager.position,
                                  sigma, seconds_to_close, external_fair)
            recorder.record_fair_value(args.ticker, now, fair_watch)
            marked_pnl = (manager.session_cash_dollars
                          + manager.position * book.mid_cents / 100.0)
            log.info("[%s] book %s/%s mid %sc | inv %+.0f | pnl $%+.2f | "
                     "quotes %s", args.ticker, book.best_bid_cents,
                     book.best_ask_cents, book.mid_cents, manager.position,
                     marked_pnl, quotes)
            if args.state_file:
                write_state(args.state_file, {
                    "ticker": args.ticker, "env": kalshi.environment,
                    "live": args.live, "ts": now,
                    "stop_ts": close_timestamp - CLOSE_BUFFER_SECONDS,
                    "mid_cents": book.mid_cents,
                    "microprice_cents": book.microprice_cents,
                    "spread_cents": book.spread_cents,
                    "book": {"bids": sorted(book.yes_bids.items(),
                                            key=lambda level: -level[0])[:8],
                             "asks": sorted((100 - no_price, quantity)
                                            for no_price, quantity
                                            in book.no_bids.items())[:8]},
                    "resting": {side: (list(order[1:]) if order else None)
                                for side, order in manager.resting.items()},
                    "position": manager.position, "pnl_dollars": marked_pnl,
                    "fill_count": len(manager.fills),
                    "fair_cents": (external_fair * 100
                                   if external_fair is not None else None),
                    "fair_age_seconds": (fair_watch.age_seconds()
                                         if fair_watch
                                         and fair_watch.fresh_fair() is not None
                                         else None),
                    "log": recent_log_lines()})
    finally:
        manager.cancel_all()
        if fair_poller is not None:
            fair_poller.stop()
        recorder.stop()
        try:
            await asyncio.wait_for(recorder_task, timeout=RECORDER_DRAIN_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
        feed.stop()
        feed_task.cancel()
        for task in (feed_task, poller_task, recorder_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def build_game_horizon(args, close_timestamp):
    if args.no_game_clock:
        return None
    parsed = market_catalog.parse_ticker(args.ticker)
    if parsed.family is None:
        log.info("[%s] no known market family; horizon falls back to "
                 "Kalshi close_time", args.ticker)
        return None

    if args.game_start:
        game_start = kalshi.parse_iso_timestamp(args.game_start)
        if not game_start:
            raise SystemExit(
                f"could not parse --game-start {args.game_start!r}; "
                f"expected ISO8601 like 2026-07-19T23:20:00Z")
        start_source = "--game-start override"
    else:
        game_start = market_catalog.ticker_start_timestamp(parsed)
        start_source = "derived from ticker"
    if not game_start:
        log.info("[%s] no start time in the ticker; horizon falls back to "
                 "Kalshi close_time", args.ticker)
        return None

    try:
        horizon = game_clock.GameClockHorizon(parsed.family.league, game_start)
    except KeyError:
        log.info("[%s] no pace profile for league %s; horizon falls back to "
                 "Kalshi close_time", args.ticker, parsed.family.league)
        return None

    now = time.time()
    estimate = horizon.estimate(now)
    elapsed_minutes = horizon.elapsed_seconds(now) / 60
    log.info("[%s] game clock: %s, started %s (%s), %.0f min elapsed, "
             "~%.0f min to game end; Kalshi close is %.0f min out",
             args.ticker, estimate.league,
             time.strftime("%H:%M", time.localtime(game_start)), start_source,
             elapsed_minutes, estimate.seconds / 60,
             (close_timestamp - now) / 60)
    if horizon.elapsed_seconds(now) > horizon.profile.nominal_real_seconds:
        log.warning("[%s] ticker start time implies the game should already "
                    "be over (%.0f min elapsed vs %.0f min typical) -- if it "
                    "is delayed, pass --game-start with the real first pitch",
                    args.ticker, elapsed_minutes,
                    horizon.profile.nominal_real_seconds / 60)
    return horizon


def horizon_seconds(horizon, close_timestamp, now):
    seconds_to_close = close_timestamp - now
    if horizon is None:
        return seconds_to_close
    return min(seconds_to_close, horizon.seconds_remaining(now))


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(
        description="Avellaneda-Stoikov market maker for one Kalshi market. "
                    "Dry-run by default; --live places real post-only "
                    "orders after a typed confirmation.")
    parser.add_argument("ticker")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--minutes", type=float, default=None)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--size", type=int, default=10)
    parser.add_argument("--max-inventory", type=int, default=50)
    parser.add_argument("--gamma", type=float, default=0.3)
    parser.add_argument("--k", type=float, default=50.0)
    parser.add_argument("--state-file", default=None, metavar="PATH",
                        help="write dashboard state here each tick "
                             "(then run dashboard.py against the same path)")
    parser.add_argument("--sgo-event", default=None, metavar="EVENT_ID",
                        help="SportsGameOdds eventID (see sgo_fairvalue.py)")
    parser.add_argument("--sgo-odd", default=None, metavar="ODD_ID",
                        help="SGO oddID whose side settles Kalshi YES")
    parser.add_argument("--sgo-poll", type=float, default=10.0)
    parser.add_argument("--sgo-line", default=None, metavar="STRIKE",
                        help="Kalshi strike; fair computed at this exact line "
                             "via bookmaker alternate lines")
    parser.add_argument("--sgo-invert", action="store_true",
                        help="use 1-p (the chosen odd settles Kalshi NO)")
    parser.add_argument("--no-record", action="store_true",
                        help="do not record this session to the database")
    parser.add_argument("--no-game-clock", action="store_true",
                        help="use Kalshi close_time as the A-S horizon "
                             "instead of the game-pace clock")
    parser.add_argument("--game-start", default=None, metavar="ISO8601",
                        help="override the game start time derived from the "
                             "ticker (use when a game is delayed)")
    parser.add_argument("--env", choices=["prod", "demo"], default=None)
    args = parser.parse_args()
    if bool(args.sgo_odd) != bool(args.sgo_event):
        raise SystemExit("--sgo-event and --sgo-odd must be given together")
    if args.env:
        kalshi.environment = args.env
    if args.live:
        typed = input(f"LIVE orders on {kalshi.environment.upper()} with real "
                      f"money. Type '{args.ticker}' to confirm: ")
        if typed.strip() != args.ticker:
            raise SystemExit("aborted")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

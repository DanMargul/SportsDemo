import logging
import time
import uuid

log = logging.getLogger("order_manager")

REPRICE_MINIMUM_MOVE_CENTS = 2
MINIMUM_PRICE_CENTS = 2
MAXIMUM_PRICE_CENTS = 98


class OrderManager:
    def __init__(self, client, ticker, max_position, max_order_contracts,
                 dry_run=True):
        self.client = client
        self.ticker = ticker
        self.max_position = max_position
        self.max_order_contracts = max_order_contracts
        self.dry_run = dry_run
        self.position = 0.0
        self.resting = {"bid": None, "ask": None}

    def rejection_reason(self, book_side, price_cents, contracts):
        if contracts > self.max_order_contracts:
            return f"size {contracts} > {self.max_order_contracts}"
        if not MINIMUM_PRICE_CENTS <= price_cents <= MAXIMUM_PRICE_CENTS:
            return (f"price {price_cents}c outside "
                    f"[{MINIMUM_PRICE_CENTS},{MAXIMUM_PRICE_CENTS}]")
        position_delta = contracts if book_side == "bid" else -contracts
        if abs(self.position + position_delta) > self.max_position:
            return f"would exceed position cap {self.max_position}"
        return None

    def sync_quotes(self, quote_pair):
        desired = {"bid": None, "ask": None}
        if quote_pair:
            if quote_pair.bid_cents:
                desired["bid"] = (quote_pair.bid_cents, quote_pair.bid_size)
            if quote_pair.ask_cents:
                desired["ask"] = (quote_pair.ask_cents, quote_pair.ask_size)
        for book_side in ("bid", "ask"):
            self.sync_side(book_side, desired[book_side])

    def sync_side(self, book_side, desired):
        resting_order = self.resting[book_side]
        if desired is None:
            if resting_order:
                self.cancel(book_side)
            return
        price_cents, contracts = desired
        reason = self.rejection_reason(book_side, price_cents, contracts)
        if reason:
            log.warning("[%s] RISK VETO (%s): %s %s @ %sc YES", self.ticker,
                        reason, book_side, contracts, price_cents)
            if resting_order:
                self.cancel(book_side)
            return
        if (resting_order
                and abs(resting_order[1] - price_cents) < REPRICE_MINIMUM_MOVE_CENTS
                and resting_order[2] == contracts):
            return
        if resting_order:
            self.cancel(book_side)
        self.place(book_side, price_cents, contracts)

    def place(self, book_side, price_cents, contracts):
        dry_tag = "[dry-run] " if self.dry_run else ""
        log.info("[%s] %sPLACE %s %s @ %sc YES", self.ticker, dry_tag,
                 book_side, contracts, price_cents)
        if self.dry_run:
            self.resting[book_side] = ("dry", price_cents, contracts)
            return
        try:
            response = self.client.create_order(
                ticker=self.ticker, book_side=book_side, contracts=contracts,
                price_cents=price_cents, client_order_id=uuid.uuid4().hex)
            self.resting[book_side] = (response["order_id"], price_cents,
                                       contracts)
        except Exception as error:
            log.error("[%s] order rejected: %s", self.ticker, error)

    def cancel(self, book_side):
        resting_order = self.resting[book_side]
        if not resting_order:
            return
        dry_tag = "[dry-run] " if self.dry_run else ""
        log.info("[%s] %sCANCEL %s @ %sc", self.ticker, dry_tag, book_side,
                 resting_order[1])
        if not self.dry_run:
            try:
                self.client.cancel_order(resting_order[0])
            except Exception as error:
                log.error("[%s] cancel failed: %s", self.ticker, error)
        self.resting[book_side] = None

    def cancel_all(self):
        self.cancel("bid")
        self.cancel("ask")

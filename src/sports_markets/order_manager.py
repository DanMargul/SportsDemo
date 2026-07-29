import logging
import time
import uuid

log = logging.getLogger("order_manager")

REPRICE_MINIMUM_MOVE_CENTS = 2
MINIMUM_PRICE_CENTS = 2
MAXIMUM_PRICE_CENTS = 98


def fill_price_cents(fill, fallback=None):
    if fill.get("yes_price_dollars") is not None:
        return round(float(fill["yes_price_dollars"]) * 100)
    if fill.get("price_dollars") is not None:
        return round(float(fill["price_dollars"]) * 100)
    if fill.get("price") is not None:
        return round(float(fill["price"]) * 100)
    if fill.get("yes_price") is not None:
        return int(fill["yes_price"])
    return fallback


def fill_direction(fill):
    outcome = fill.get("outcome_side") or {
        "bid": "yes", "ask": "no"}.get(fill.get("book_side"))
    if outcome is not None:
        return outcome, (1 if outcome == "yes" else -1)
    sign = 1 if fill.get("side") == "yes" else -1
    if fill.get("action") == "sell":
        sign = -sign
    return f"{fill.get('action')}/{fill.get('side')}", sign


class OrderManager:
    def __init__(self, client, ticker, max_position, max_order_contracts,
                 dry_run=True, observer=None):
        self.client = client
        self.observer = observer
        self.ticker = ticker
        self.max_position = max_position
        self.max_order_contracts = max_order_contracts
        self.dry_run = dry_run
        self.position = 0.0
        self.session_cash_dollars = 0.0
        self.fills = []
        self.resting = {"bid": None, "ask": None}

    def notify(self, event, **payload):
        if self.observer is None:
            return
        handler = getattr(self.observer, event, None)
        if handler is None:
            return
        try:
            handler(**payload)
        except Exception as error:
            log.debug("[%s] observer %s failed: %s", self.ticker, event, error)

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
            order_id = uuid.uuid4().hex
            self.resting[book_side] = (order_id, price_cents, contracts)
            self.notify("on_order_placed", ticker=self.ticker,
                        order_id=order_id, book_side=book_side,
                        price_cents=price_cents, contracts=contracts,
                        status="dry_run")
            return
        try:
            response = self.client.create_order(
                ticker=self.ticker, book_side=book_side, contracts=contracts,
                price_cents=price_cents, client_order_id=uuid.uuid4().hex)
            self.resting[book_side] = (response["order_id"], price_cents,
                                       contracts)
            self.notify("on_order_placed", ticker=self.ticker,
                        order_id=response["order_id"], book_side=book_side,
                        price_cents=price_cents, contracts=contracts,
                        status="resting")
        except Exception as error:
            log.error("[%s] order rejected: %s", self.ticker, error)
            self.notify("on_order_placed", ticker=self.ticker, order_id=None,
                        book_side=book_side, price_cents=price_cents,
                        contracts=contracts, status="rejected",
                        reject_reason=str(error)[:500])

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
        self.notify("on_order_cancelled", ticker=self.ticker,
                    order_id=resting_order[0])
        self.resting[book_side] = None

    def cancel_all(self):
        self.cancel("bid")
        self.cancel("ask")

    def apply_fill(self, fill):
        contracts = float(fill.get("count_fp", fill.get("count", 0)) or 0)
        outcome_label, sign = fill_direction(fill)
        self.position += sign * contracts
        resting_order = self.resting["bid" if sign > 0 else "ask"]
        price_cents = fill_price_cents(
            fill, fallback=resting_order[1] if resting_order else None)
        if price_cents is not None:
            self.session_cash_dollars -= (sign * (price_cents / 100.0)
                                          * contracts)
        self.fills.append({"ts": time.time(), "ticker": self.ticker,
                           "outcome": outcome_label, "price_cents": price_cents,
                           "contracts": contracts})
        log.info("[%s] FILL %s x%s @ %sc -> position %+.0f", self.ticker,
                 outcome_label, contracts, price_cents, self.position)
        self.notify("on_fill", ticker=self.ticker, fill=fill,
                    price_cents=price_cents, contracts=int(round(contracts)),
                    book_side="bid" if sign > 0 else "ask")

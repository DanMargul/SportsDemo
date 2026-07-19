from dataclasses import dataclass, field


def parse_price_levels(message: dict, side_key: str) -> dict:
    dollar_rows = message.get(f"{side_key}_dollars")
    rows = dollar_rows if dollar_rows is not None else (message.get(side_key) or [])
    levels = {}
    for price, quantity_value in rows:
        if dollar_rows is not None:
            dollars = float(price)
            cents = round(dollars * 100)
            if abs(dollars * 100 - cents) > 1e-6:
                continue
            price_cents = cents
        else:
            price_cents = int(price)
        quantity = float(quantity_value)
        if quantity > 0:
            levels[price_cents] = levels.get(price_cents, 0.0) + quantity
    return levels


@dataclass
class OrderBook:
    ticker: str = ""
    yes_bids: dict = field(default_factory=dict)
    no_bids: dict = field(default_factory=dict)
    has_snapshot: bool = False

    @classmethod
    def from_rest(cls, ticker: str, incoming_data: dict) -> "OrderBook":
        body = incoming_data.get("orderbook_fp") or incoming_data.get("orderbook") or {}
        book = cls(ticker=ticker)
        book.apply_snapshot(body)
        return book

    def apply_snapshot(self, message: dict) -> None:
        self.yes_bids = parse_price_levels(message, "yes")
        self.no_bids = parse_price_levels(message, "no")
        self.has_snapshot = True

    @property
    def best_bid_cents(self):
        if not self.yes_bids:
            return None
        return max(self.yes_bids)

    @property
    def best_ask_cents(self):
        if not self.no_bids:
            return None
        return 100 - max(self.no_bids)

    @property
    def mid_cents(self):
        bid, ask = self.best_bid_cents, self.best_ask_cents
        if bid is None:
            return None
        if ask is None:
            return None
        return (bid + ask) / 2

    @property
    def spread_cents(self):
        bid, ask = self.best_bid_cents, self.best_ask_cents
        if bid is None:
            return None
        if ask is None:
            return None
        return ask - bid

    @property
    def microprice_cents(self):
        bid, ask = self.best_bid_cents, self.best_ask_cents
        if bid is None:
            return None
        if ask is None:
            return None
        bid_quantity = self.yes_bids[bid]
        ask_quantity = self.no_bids[100 - ask]
        if bid_quantity + ask_quantity <= 0:
            return (bid + ask) / 2
        microprice =  ask_quantity * bid
        microprice += bid_quantity * ask
        microprice /= bid_quantity + ask_quantity
        return microprice

    def ladder_str(self, levels: int = 6) -> str:
        asks = sorted(((100 - no_price, quantity)
                       for no_price, quantity in self.no_bids.items()))[:levels]
        bids = sorted(self.yes_bids.items(), key=lambda level: -level[0])[:levels]
        lines = [f"--- {self.ticker} (YES) ---"]
        lines += [f"   ASK {price:>3}c x {quantity:>9.0f}"
                  for price, quantity in reversed(asks)]
        lines.append(f"   ---- spread {self.spread_cents}c ----")
        lines += [f"   BID {price:>3}c x {quantity:>9.0f}"
                  for price, quantity in bids]
        return "\n".join(lines)

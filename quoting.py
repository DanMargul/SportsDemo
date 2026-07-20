import math
import time
from dataclasses import dataclass
from typing import Optional


class VolatilityEWMA:
    def __init__(self, half_life_seconds: float = 300.0,
                 initial_sigma: float = 1e-4):
        self.half_life_seconds = half_life_seconds
        self.variance_per_second = initial_sigma ** 2
        self.previous_mid = None
        self.previous_timestamp = None

    def update(self, mid_probability: float, timestamp: float = None):
        if timestamp is None:
            timestamp = time.time()
        if self.previous_mid is not None:
            elapsed_seconds = timestamp - self.previous_timestamp
            if elapsed_seconds > 1e-3:
                squared_return_per_second = (
                    (mid_probability - self.previous_mid) ** 2 / elapsed_seconds)
                blend = 1 - 0.5 ** (elapsed_seconds / self.half_life_seconds)
                self.variance_per_second += blend * (
                    squared_return_per_second - self.variance_per_second)
        self.previous_mid = mid_probability
        self.previous_timestamp = timestamp

    def sigma_per_sqrt_second(self) -> float:
        return math.sqrt(max(self.variance_per_second, 1e-12))


@dataclass
class QuoteConfig:
    risk_aversion: float = 0.3
    fill_intensity_decay: float = 50.0
    quote_size: int = 10
    max_inventory: int = 50
    horizon_cap_seconds: float = 1800.0
    min_half_spread_cents: float = 1.0
    max_half_spread_cents: float = 8.0
    external_fair_weight: float = 0.7


@dataclass
class QuotePair:
    bid_cents: Optional[int]
    bid_size: int
    ask_cents: Optional[int]
    ask_size: int

    def __str__(self):
        bid = f"{self.bid_cents}c x{self.bid_size}" if self.bid_cents else "-"
        ask = f"{self.ask_cents}c x{self.ask_size}" if self.ask_cents else "-"
        return f"bid[{bid}] ask[{ask}]"


def blended_fair_probability(book, config, external_fair_probability):
    book_fair_cents = (book.microprice_cents if book.microprice_cents is not None
                       else book.mid_cents)
    if book_fair_cents is None:
        return external_fair_probability
    book_fair_probability = book_fair_cents / 100.0
    if external_fair_probability is None:
        return book_fair_probability
    weight = config.external_fair_weight
    return (weight * external_fair_probability
            + (1 - weight) * book_fair_probability)


def compute_quotes(book, inventory, sigma_per_sqrt_second, seconds_to_close,
                   config, external_fair_probability=None):
    fair_probability = blended_fair_probability(book, config,
                                                external_fair_probability)
    if fair_probability is None:
        return None

    horizon_seconds = max(min(seconds_to_close, config.horizon_cap_seconds), 1.0)
    variance_over_horizon = sigma_per_sqrt_second ** 2 * horizon_seconds
    reservation_probability = (
        fair_probability
        - inventory * config.risk_aversion * variance_over_horizon)
    half_spread_probability = (
        0.5 * config.risk_aversion * variance_over_horizon
        + math.log(1 + config.risk_aversion / config.fill_intensity_decay)
        / config.risk_aversion)
    half_spread_probability = min(
        max(half_spread_probability, config.min_half_spread_cents / 100),
        config.max_half_spread_cents / 100)

    bid_cents = math.floor((reservation_probability - half_spread_probability) * 100)
    ask_cents = math.ceil((reservation_probability + half_spread_probability) * 100)

    fair_cents = fair_probability * 100
    bid_cents = min(bid_cents, math.floor(fair_cents) - 1)
    ask_cents = max(ask_cents, math.ceil(fair_cents) + 1)
    if book.best_ask_cents is not None:
        bid_cents = min(bid_cents, book.best_ask_cents - 1)
    if book.best_bid_cents is not None:
        ask_cents = max(ask_cents, book.best_bid_cents + 1)
    bid_cents = max(bid_cents, 1)
    ask_cents = min(ask_cents, 99)
    if ask_cents <= bid_cents:
        ask_cents = bid_cents + 1

    quote_bid = inventory < config.max_inventory and bid_cents >= 1
    quote_ask = inventory > -config.max_inventory and ask_cents <= 99
    if not (quote_bid or quote_ask):
        return None
    return QuotePair(bid_cents=bid_cents if quote_bid else None,
                     bid_size=config.quote_size,
                     ask_cents=ask_cents if quote_ask else None,
                     ask_size=config.quote_size)

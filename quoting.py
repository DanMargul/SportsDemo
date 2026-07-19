import math
import time


class EwmaVolatility:
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

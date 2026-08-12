import base64
import os
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

kalshi_api_root = "/trade-api/v2"
rest_api_url = {
    "demo": "https://demo-api.kalshi.co",
    "prod": "https://api.elections.kalshi.com"}
websocket_url = {
    "demo": "wss://demo-api.kalshi.co/trade-api/ws/v2",
    "prod": "wss://api.elections.kalshi.com/trade-api/ws/v2"}
websocket_sign_path = "/trade-api/ws/v2"

access_key_id = os.environ.get(key="KALSHI_API_KEY_ID")
private_key_path = os.environ.get(key="KALSHI_PRIVATE_KEY_PATH")
environment = os.environ.get(key="KALSHI_ENV",
                             default="prod").lower()

kalshi_private_key = None
def private_key():
    global kalshi_private_key
    if not kalshi_private_key:
        if not access_key_id:
            raise RuntimeError("Set environment variable KALSHI_API_KEY_ID.")
        if not private_key_path:
            raise RuntimeError("Set environment variable KALSHI_PRIVATE_KEY_PATH")
        with open(private_key_path, "rb") as private_key_file:
            kalshi_private_key = serialization.load_pem_private_key(
                private_key_file.read(), password=None
            )
    return kalshi_private_key

def signed_headers(method: str, path: str) -> dict:
    time_now_milliseconds = str(int(time.time() * 1000))
    if path.startswith("/trade-api"):
        full_path = path
    else:
        full_path = kalshi_api_root + path

    send_text = (f"{time_now_milliseconds}"
                 f"{method.upper()}"
                 f"{full_path.split('?')[0]}")
    send_signature = private_key().sign(
        send_text.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256()
    )
    return_dict = {
        "KALSHI-ACCESS-KEY": access_key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(send_signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": time_now_milliseconds
    }
    return return_dict


def parse_iso_timestamp(value) -> float:
    if not value:
        return 0.0
    from datetime import datetime, timezone
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0.0


class KalshiClient:
    def __init__(self, timeout_seconds: float = 20.0,
                 min_request_interval_seconds: float = 0.1):
        self.session = requests.Session()
        self.timeout_seconds = timeout_seconds
        self.prior_request_time_monotonic = 0.0
        self.min_request_interval_seconds = min_request_interval_seconds


    def request_json(self, method: str, path: str,
                     params=None, body=None, signed=False):

        wait_seconds = self.min_request_interval_seconds
        wait_seconds -= (time.monotonic() - self.prior_request_time_monotonic)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        self.prior_request_time_monotonic = time.monotonic()

        headers = {"Accept": "application/json"}
        if signed:
            headers.update(signed_headers(method, path))
        response = self.session.request(method,
                                        rest_api_url[environment] + kalshi_api_root + path,
                                        params=params,
                                        json=body,
                                        headers=headers,
                                        timeout=self.timeout_seconds)
        if not response.ok:
            raise RuntimeError(
                f"HTTP {response.status_code} {path}: {response.text[:300]}"
            )
        if not response.text:
            return {}
        return response.json()

    def get_exchange_status(self):
        return self.request_json(method="GET",
                                 path="/exchange/status")

    def get_market(self, ticker: str):
        return self.request_json(method="GET",
                                 path=f"/markets/{ticker}").get("market", {})

    def get_markets(self, *, series_ticker=None, event_ticker=None,
                    status=None, max_markets=500):
        collected = []
        cursor = None
        while len(collected) < max_markets:
            params = {"limit": 200}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if event_ticker:
                params["event_ticker"] = event_ticker
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            payload = self.request_json(method="GET", path="/markets",
                                        params=params)
            collected.extend(payload.get("markets", []))
            cursor = payload.get("cursor")
            if not cursor or not payload.get("markets"):
                break
        return collected[:max_markets]

    def get_orderbook(self, ticker, depth_levels=50):
        return self.request_json(method="GET",
                                 path=f"/markets/{ticker}/orderbook",
                                 params={"depth": depth_levels})

    def get_trades(self, ticker, maximum_trades=200):
        return self.request_json(
            method="GET", path="/markets/trades",
            params={"ticker": ticker, "limit": maximum_trades}).get("trades", [])

    def get_balance(self):
        return self.request_json(method="GET",
                                 path="/portfolio/balance",
                                 signed=True)

    def get_position(self, ticker) -> float:
        payload = self.request_json(method="GET",
                                    path="/portfolio/positions",
                                    params={"ticker": ticker}, signed=True)
        for market_position in payload.get("market_positions", []):
            if market_position.get("ticker") == ticker:
                return float(market_position.get("position", 0))
        return 0.0

    def create_order(self, *, ticker, book_side, contracts, price_cents,
                     client_order_id):
        body = {"ticker": ticker,
                "side": book_side,
                "count": str(int(contracts)),
                "price": f"{price_cents / 100:.2f}",
                "client_order_id": client_order_id,
                "time_in_force": "good_till_canceled",
                "self_trade_prevention_type": "taker_at_cross",
                "post_only": True,
                "cancel_order_on_pause": True}
        return self.request_json(method="POST",
                                 path="/portfolio/events/orders",
                                 body=body, signed=True)

    def cancel_order(self, order_id):
        return self.request_json(method="DELETE",
                                 path=f"/portfolio/events/orders/{order_id}",
                                 signed=True)
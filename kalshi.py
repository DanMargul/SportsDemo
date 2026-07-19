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
                                 path=f"/markets/{ticker}")

    def get_balance(self):
        return self.request_json(method="GET",
                                 path="/portfolio/balance",
                                 signed=True)
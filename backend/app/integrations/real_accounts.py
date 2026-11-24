"""Fixed, read-only connections to the official account endpoints."""

from decimal import Decimal

import httpx

from .common import VenueError, decimal, identifier, number, object_, request_json
from .deriv import account_headers, read_option_accounts
from .kalshi import KalshiSigner

_REAL_KALSHI_BALANCE_URL = "https://external-api.kalshi.com/trade-api/v2/portfolio/balance"
_REAL_KALSHI_BALANCE_PATH = "/trade-api/v2/portfolio/balance"


class DerivRealReader:
    def __init__(self, app_id: str, token: str, account_id: str, *, _transport=None):
        self._credentials = app_id, token
        self._account_id = identifier(account_id)
        self._transport = _transport

    async def verify(self) -> tuple[str, Decimal]:
        accounts = await read_option_accounts(
            lambda: account_headers(*self._credentials), self._transport,
        )
        selected = []
        for raw in accounts:
            row = object_(raw)
            if row.get("account_id") == self._account_id:
                selected.append(row)
        if len(selected) != 1:
            raise VenueError("REAL_ACCOUNT_REQUIRED")
        account = selected[0]
        if account.get("account_type") != "real" or account.get("status") != "active":
            raise VenueError("REAL_ACCOUNT_REQUIRED")
        currency = identifier(account.get("currency"), r"[A-Z0-9]{2,20}")
        balance = decimal(account.get("balance"))
        if balance < 0:
            raise VenueError("INVALID_RESPONSE")
        return currency, balance


class KalshiRealReader:
    def __init__(self, key_id: str, private_key: str, *, _transport: httpx.AsyncBaseTransport | None = None):
        self._auth = KalshiSigner(key_id, private_key)
        self._transport = _transport

    async def verify(self) -> tuple[str, Decimal]:
        row = await request_json(
            "GET",
            _REAL_KALSHI_BALANCE_URL,
            transport=self._transport,
            auth=lambda: self._auth.headers("GET", _REAL_KALSHI_BALANCE_PATH),
            pace_seconds=1,
            pace_bucket="kalshi:real-portfolio-balance",
        )
        fixed_balance = row.get("balance_dollars")
        balance = decimal(fixed_balance) if fixed_balance is not None else Decimal(number(row.get("balance"))) / 100
        if balance < 0:
            raise VenueError("INVALID_RESPONSE")
        return "USD", balance

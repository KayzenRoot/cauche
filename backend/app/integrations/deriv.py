"""Deriv Options API using public market data and authenticated demo-only execution."""

import asyncio
import itertools
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
from websockets.exceptions import WebSocketException

from .common import (
    NoRedirectConnect,
    VenueError,
    array,
    bar,
    decimal,
    identifier,
    number,
    object_,
    optional_decimal,
    pace,
    receive,
    request_json,
    socket_session,
    string,
)

_HOST = "api.derivws.com"
_ACCOUNTS = f"https://{_HOST}/trading/v1/options/accounts"
_PUBLIC_WS = f"wss://{_HOST}/trading/v1/options/ws/public"
_DEMO_PATH = "/trading/v1/options/ws/demo"
_INTERVALS = {60, 120, 180, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400}
_TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_SYMBOL = r"[A-Za-z0-9_]{2,30}"
_ACCOUNT_BUCKET = "deriv:authenticated-rest"
_MINIMUM_INTERVAL = {
    "balance": ("deriv:balance", 2),
    "statement": ("deriv:balance", 2),
    "portfolio": ("deriv:portfolio", 2.5),
    "proposal": ("deriv:trading", 0.3),
    "buy": ("deriv:trading", 0.3),
    "proposal_open_contract": ("deriv:trading", 0.3),
}
_KNOWN_REJECTIONS = {
    "AuthorizationRequired",
    "ContractCreationFailure",
    "InsufficientBalance",
    "InvalidContractProposal",
    "InvalidToken",
    "MarketIsClosed",
    "PriceMoved",
    "RateLimit",
}


def account_headers(app_id: str | None, token: str | None) -> dict[str, str]:
    if not app_id or not token:
        raise VenueError("NOT_CONFIGURED")
    if (
        re.fullmatch(r"[A-Za-z0-9_-]{1,100}", app_id) is None
        or re.fullmatch(r"[\x21-\x7e]{1,4096}", token) is None
    ):
        raise VenueError("AUTH_ERROR")
    return {"Deriv-App-ID": app_id, "Authorization": f"Bearer {token}"}


async def read_option_accounts(auth, transport=None) -> list:
    payload = await request_json(
        "GET", _ACCOUNTS, auth=auth, transport=transport,
        pace_seconds=1, pace_bucket=_ACCOUNT_BUCKET,
    )
    return array(payload.get("data"))


class Deriv:
    def __init__(
        self,
        app_id: str | None = None,
        token: str | None = None,
        account_id: str | None = None,
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
        _connect: Callable[..., Any] = NoRedirectConnect,
    ) -> None:
        self._app_id = app_id
        self._token = token
        self._account_id = account_id
        self._transport = _transport
        self._connect = _connect
        self._request_ids = itertools.count(1)

    def _headers(self) -> dict[str, str]:
        return account_headers(self._app_id, self._token)

    @staticmethod
    def _demo_url(raw: Any) -> str:
        try:
            url = urlsplit(string(raw))
            query = parse_qsl(url.query, strict_parsing=True, max_num_fields=1)
            if (
                not raw.startswith(f"wss://{_HOST}{_DEMO_PATH}?")
                or url.scheme != "wss"
                or url.netloc != _HOST
                or url.path != _DEMO_PATH
                or "#" in raw
                or len(query) != 1
                or query[0][0] != "otp"
                or re.fullmatch(r"[A-Za-z0-9._~+/%=-]{1,4096}", url.query[4:]) is None
                or not re.fullmatch(r"[\x21-\x7e]{1,4096}", query[0][1])
            ):
                raise ValueError()
        except (ValueError, VenueError):
            raise VenueError("DEMO_DESTINATION_BLOCKED") from None
        return raw

    async def _demo_target(self) -> tuple[str, dict]:
        selected = identifier(self._account_id) if self._account_id else None
        eligible = []
        for raw in await read_option_accounts(self._headers, self._transport):
            entry = object_(raw)
            if entry.get("account_type") != "demo" or entry.get("status") != "active":
                continue
            account = identifier(string(entry.get("account_id")))
            if selected is None or account == selected:
                eligible.append(
                    (
                        account,
                        identifier(string(entry.get("currency")), r"[A-Z0-9]{2,20}"),
                    )
                )
        if len(eligible) != 1:
            raise VenueError("DEMO_ACCOUNT_REQUIRED")
        account, currency = eligible[0]
        otp = await request_json(
            "POST",
            f"{_ACCOUNTS}/{account}/otp",
            auth=self._headers,
            transport=self._transport,
            pace_seconds=1,
            pace_bucket=_ACCOUNT_BUCKET,
        )
        return self._demo_url(object_(otp.get("data")).get("url")), {
            "account_id": account,
            "currency": currency,
        }

    @asynccontextmanager
    async def _demo_socket(self) -> AsyncIterator[tuple[Any, dict]]:
        url, account = await self._demo_target()
        async with socket_session(url, self._connect) as websocket:
            yield websocket, account

    async def _rpc(
        self,
        websocket: Any,
        command: str,
        payload: dict,
        *,
        mutation=False,
        proposal_stream: str | None = None,
    ) -> Any:
        request_id = next(self._request_ids)
        await pace(*_MINIMUM_INTERVAL.get(command, ("deriv:market", 0.35)))
        try:
            async with asyncio.timeout(8):
                await websocket.send(json.dumps({**payload, "req_id": request_id}))
                response = await receive(websocket, timeout=8)
                while (
                    proposal_stream is not None
                    and response.get("msg_type") == "proposal"
                    and isinstance(response.get("subscription"), dict)
                    and response["subscription"].get("id") == proposal_stream
                ):
                    response = await receive(websocket, timeout=8)
        except (WebSocketException, OSError, TimeoutError):
            raise VenueError(
                "DISCONNECTED", retryable=not mutation, outcome_unknown=mutation
            ) from None
        except VenueError as exc:
            raise VenueError(exc.code, outcome_unknown=mutation) from None
        if type(response.get("req_id")) is not int or response["req_id"] != request_id:
            raise VenueError("UNCORRELATED_RESPONSE", outcome_unknown=mutation)
        if response.get("error") is not None:
            error = response["error"]
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str):
                raise VenueError("INVALID_RESPONSE", outcome_unknown=mutation)
            if code in {"AuthorizationRequired", "InvalidToken"}:
                raise VenueError("AUTH_ERROR")
            if code == "RateLimit":
                raise VenueError("RATE_LIMIT", retryable=not mutation)
            raise VenueError(
                "UPSTREAM_REJECTED",
                outcome_unknown=mutation and code not in _KNOWN_REJECTIONS,
            )
        if response.get("msg_type") != command or command not in response:
            raise VenueError("INVALID_RESPONSE", outcome_unknown=mutation)
        if command == "proposal" and payload.get("subscribe") == 1:
            subscription = object_(response.get("subscription"))
            if subscription.get("id") != string(
                object_(response["proposal"]).get("id")
            ):
                raise VenueError("INVALID_RESPONSE")
        return response[command]

    async def markets(self) -> list[dict]:
        async with socket_session(_PUBLIC_WS, self._connect) as websocket:
            rows = await self._rpc(
                websocket, "active_symbols", {"active_symbols": "brief"}
            )
        return [
            {
                "instrument": identifier(string(row.get("underlying_symbol")), _SYMBOL),
                "name": string(row.get("underlying_symbol_name")),
                "category": string(row.get("market")),
                "is_open": row.get("exchange_is_open") == 1
                and row.get("is_trading_suspended") == 0,
                "source": "DERIV_PUBLIC",
            }
            for row in map(object_, array(rows))
        ]

    async def candles(
        self, instrument: str, interval_seconds: int = 300, limit: int = 200
    ) -> list[dict]:
        if interval_seconds not in _INTERVALS or not 1 <= limit <= 1000:
            raise VenueError("INVALID_HISTORY_RANGE")
        payload = {
            "ticks_history": identifier(instrument, _SYMBOL),
            "end": "latest",
            "style": "candles",
            "granularity": interval_seconds,
            "count": limit,
        }
        async with socket_session(_PUBLIC_WS, self._connect) as websocket:
            rows = await self._rpc(websocket, "candles", payload)
        now = int(time.time())
        result = []
        for raw in map(object_, array(rows)):
            opened = number(raw.get("epoch"))
            if opened + interval_seconds <= now:
                result.append(bar(opened, raw))
        return _ordered_unique(result)

    async def history_ticks(self, instrument: str, limit: int = 200) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise VenueError("INVALID_HISTORY_RANGE")
        payload = {
            "ticks_history": identifier(instrument, _SYMBOL),
            "end": "latest",
            "style": "ticks",
            "count": limit,
        }
        async with socket_session(_PUBLIC_WS, self._connect) as websocket:
            history = object_(await self._rpc(websocket, "history", payload))
        times = array(history.get("times"))
        prices = array(history.get("prices"))
        if len(times) != len(prices):
            raise VenueError("INVALID_RESPONSE")
        return [
            {
                "instrument": instrument,
                "observed_at": number(timestamp),
                "price": decimal(price),
                "source": "DERIV_PUBLIC",
            }
            for timestamp, price in zip(times, prices, strict=True)
        ]

    async def stream_ticks(self, instrument: str) -> AsyncIterator[dict]:
        """Read-only stream. Each retry begins with a fresh subscription."""
        symbol = identifier(instrument, _SYMBOL)
        delay = 1
        while True:
            try:
                async with socket_session(_PUBLIC_WS, self._connect) as websocket:
                    first = await self._rpc(
                        websocket, "tick", {"ticks": symbol, "subscribe": 1}
                    )
                    yield _tick(symbol, first)
                    delay = 1
                    while True:
                        message = await receive(websocket)
                        if message.get("msg_type") != "tick":
                            raise VenueError("INVALID_RESPONSE")
                        yield _tick(symbol, message.get("tick"))
            except VenueError as exc:
                if not exc.retryable:
                    raise
                yield {
                    "status": exc.code,
                    "instrument": symbol,
                    "observed_at": int(time.time()),
                }
                await asyncio.sleep(max(delay, exc.retry_after or 0))
                delay = min(delay * 2, 30)

    async def contracts(self, instrument: str) -> list[dict]:
        symbol = identifier(instrument, _SYMBOL)
        async with socket_session(_PUBLIC_WS, self._connect) as websocket:
            return await self._contracts(websocket, symbol)

    async def _contracts(self, websocket: Any, symbol: str) -> list[dict]:
        payload = object_(
            await self._rpc(websocket, "contracts_for", {"contracts_for": symbol})
        )
        return [
            {
                "contract_type": item.get("contract_type"),
                "category": item.get("contract_category"),
                "min_duration": item.get("min_contract_duration"),
                "max_duration": item.get("max_contract_duration"),
                "barriers": item.get("barriers"),
            }
            for item in map(object_, array(payload.get("available")))
            if item.get("underlying_symbol") == symbol
        ]

    async def _quote(
        self,
        websocket: Any,
        instrument: str,
        side: str,
        stake: Decimal,
        duration: int,
        duration_unit: str,
        currency: str,
        *,
        streaming: bool = False,
    ) -> tuple[str, dict]:
        symbol = identifier(instrument, _SYMBOL)
        amount = decimal(stake)
        if (
            side not in {"CALL", "PUT"}
            or not 1 <= duration <= 10080
            or duration_unit not in _TIME_UNITS
        ):
            raise VenueError("INVALID_CONTRACT")
        if not Decimal("0.01") <= amount <= 10000 or amount != amount.quantize(
            Decimal("0.01")
        ):
            raise VenueError("INVALID_STAKE")
        available = await self._contracts(websocket, symbol)
        duration_seconds = duration * _TIME_UNITS[duration_unit]
        if not any(
            contract["contract_type"] == side
            and contract["category"] == "callput"
            and contract["barriers"] in {0, 1}
            and _duration_seconds(
                contract["min_duration"],
                fallback=duration_unit,
                default=duration_seconds + 1,
            )
            <= duration_seconds
            <= _duration_seconds(
                contract["max_duration"], fallback=duration_unit, default=0
            )
            for contract in available
        ):
            raise VenueError("CONTRACT_UNAVAILABLE")
        payload = {
            "proposal": 1,
            "basis": "stake",
            "amount": _wire_number(amount),
            "contract_type": side,
            "currency": currency,
            "duration": duration,
            "duration_unit": duration_unit,
            "underlying_symbol": symbol,
        }
        # Omitting the barrier is the Rise/Fall proposal for an entry-spot
        # threshold. Contracts-for also lists the optional Higher/Lower barrier.
        if streaming:
            # The current API only accepts a streaming proposal ID for buy.
            payload["subscribe"] = 1
        result = object_(await self._rpc(websocket, "proposal", payload))
        quote_id = string(result.get("id"))
        price = decimal(result.get("ask_price"))
        payout = decimal(result.get("payout"))
        observed_at = number(result.get("spot_time"))
        spot = decimal(result.get("spot"))
        if not 0 < price <= amount or not price < payout or spot <= 0:
            raise VenueError("QUOTE_RISK_BLOCKED")
        if not 0 <= time.time() - observed_at <= 10:
            raise VenueError("STALE_QUOTE")
        return quote_id, {
            "stake": price,
            "payout": payout,
            "spot": spot,
            "observed_at": observed_at,
            "expires_at": result.get("date_expiry"),
            "currency": currency,
            "source": "DERIV_PROPOSAL",
        }

    async def quote(
        self,
        instrument: str,
        side: str,
        stake: Decimal,
        *,
        duration: int = 5,
        duration_unit: str = "m",
        currency: str = "USD",
    ) -> dict:
        identifier(currency, r"[A-Z0-9]{2,20}")
        async with socket_session(_PUBLIC_WS, self._connect) as websocket:
            _, quote = await self._quote(
                websocket, instrument, side, stake, duration, duration_unit, currency
            )
            return quote

    async def _balance(self, websocket: Any, account: dict) -> dict:
        balance = object_(await self._rpc(websocket, "balance", {"balance": 1}))
        if (
            balance.get("loginid") != account["account_id"]
            or balance.get("currency") != account["currency"]
        ):
            raise VenueError("DEMO_ACCOUNT_MISMATCH")
        amount = decimal(balance.get("balance"))
        if amount < 0:
            raise VenueError("INVALID_RESPONSE")
        return {**account, "balance": amount, "observed_at": int(time.time())}

    async def verify_demo(self) -> dict:
        async with self._demo_socket() as (websocket, account):
            balance = await self._balance(websocket, account)
        return {
            **balance,
            "environment": "BROKER_DEMO",
            "source": "DERIV_DEMO",
            "capabilities": ["ACCOUNT_DISCOVERY", "DEMO_SOCKET", "BALANCE_READ"],
        }

    async def submit_demo(
        self,
        instrument: str,
        side: str,
        stake: Decimal,
        *,
        client_order_id: str,
        duration: int = 5,
        duration_unit: str = "m",
        min_payout_ratio: Decimal = Decimal("1.60"),
        max_spot_change: Decimal | None = None,
        expected_spot: Decimal | None = None,
        expected_currency: str = "USD",
    ) -> dict:
        identifier(client_order_id, r"[A-Za-z0-9_-]{1,64}")
        identifier(expected_currency, r"[A-Z0-9]{2,20}")
        payout_floor = decimal(min_payout_ratio)
        if payout_floor < 1:
            raise VenueError("INVALID_PAYOUT_FLOOR")
        async with self._demo_socket() as (websocket, account):
            if account["currency"] != expected_currency:
                raise VenueError("UNSUPPORTED_CURRENCY")
            balance = await self._balance(websocket, account)
            quote_id, quote = await self._quote(
                websocket,
                instrument,
                side,
                stake,
                duration,
                duration_unit,
                account["currency"],
                streaming=True,
            )
            ask = quote["stake"]
            if quote["payout"] < ask * payout_floor or balance["balance"] < ask:
                raise VenueError("QUOTE_RISK_BLOCKED")
            if expected_spot is not None and max_spot_change is not None:
                if abs(quote["spot"] - decimal(expected_spot)) > decimal(
                    max_spot_change
                ):
                    raise VenueError("PRICE_MOVED")
            result = await self._rpc(
                websocket,
                "buy",
                {
                    "buy": quote_id,
                    "price": _wire_number(ask),
                    "passthrough": {"client_order_id": client_order_id},
                },
                mutation=True,
                proposal_stream=quote_id,
            )
            try:
                purchase = object_(result)
                price = decimal(purchase.get("buy_price"))
                payout = decimal(purchase.get("payout"))
                if not 0 < price <= ask or payout < price * payout_floor:
                    raise VenueError("BROKER_QUOTE_DIVERGENCE")
                return {
                    "broker_id": str(number(purchase.get("contract_id"))),
                    "transaction_id": str(number(purchase.get("transaction_id"))),
                    "client_order_id": client_order_id,
                    "status": "OPEN",
                    "price": price,
                    "quantity": Decimal(1),
                    "payout": payout,
                    "balance": decimal(purchase.get("balance_after")),
                    "currency": account["currency"],
                    "observed_at": number(purchase.get("purchase_time")),
                    "source": "DERIV_DEMO",
                }
            except VenueError as exc:
                raise VenueError(exc.code, outcome_unknown=True) from None

    async def reconcile_demo(self, broker_id: str) -> dict:
        contract = int(identifier(broker_id, r"[1-9][0-9]{0,18}"))
        async with self._demo_socket() as (websocket, account):
            await self._balance(websocket, account)
            payload = await self._rpc(
                websocket,
                "proposal_open_contract",
                {"proposal_open_contract": 1, "contract_id": contract},
            )
        return _contract_state(payload, contract, account)

    async def portfolio(self) -> list[dict]:
        async with self._demo_socket() as (websocket, account):
            await self._balance(websocket, account)
            payload = object_(await self._rpc(websocket, "portfolio", {"portfolio": 1}))
        return [
            {
                "broker_id": str(number(row.get("contract_id"))),
                "instrument": identifier(string(row.get("underlying_symbol")), _SYMBOL),
                "side": string(row.get("contract_type")),
                "price": decimal(row.get("buy_price")),
                "payout": decimal(row.get("payout")),
                "observed_at": number(row.get("purchase_time")),
            }
            for row in map(object_, array(payload.get("contracts")))
        ]

    async def statement(
        self, *, date_from: int, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        """Provide venue transaction IDs for manual recovery of ambiguous purchases."""
        if (
            not 0 < date_from <= int(time.time())
            or not 1 <= limit <= 100
            or not 0 <= offset <= 10000
        ):
            raise VenueError("INVALID_HISTORY_RANGE")
        async with self._demo_socket() as (websocket, account):
            await self._balance(websocket, account)
            payload = object_(
                await self._rpc(
                    websocket,
                    "statement",
                    {
                        "statement": 1,
                        "description": 1,
                        "date_from": date_from,
                        "limit": limit,
                        "offset": offset,
                    },
                )
            )
        return [
            {
                "transaction_id": str(number(row.get("transaction_id"))),
                "broker_id": str(number(row.get("contract_id"))),
                "app_id": row.get("app_id"),
                "shortcode": row.get("shortcode"),
                "action": row["action_type"],
                "amount": decimal(row.get("amount")),
                "observed_at": number(row.get("transaction_time")),
                "currency": account["currency"],
                "source": "DERIV_DEMO",
            }
            for row in map(object_, array(payload.get("transactions", [])))
            if row.get("action_type") in {"buy", "sell"}
        ]


def _ordered_unique(rows: list[dict]) -> list[dict]:
    ordered = sorted(rows, key=lambda row: row["time"])
    if any(left["time"] == right["time"] for left, right in zip(ordered, ordered[1:])):
        raise VenueError("INVALID_RESPONSE")
    return ordered


def _wire_number(value: Decimal) -> float:
    wire = float(value)
    if Decimal(str(wire)) != value:
        raise VenueError("INVALID_PRECISION")
    return wire


def _duration_seconds(value: Any, *, fallback: str, default: int) -> int:
    match = re.fullmatch(r"([0-9]{1,9})([smhd])?", str(value))
    if match is None:
        return default
    return int(match[1]) * _TIME_UNITS[match[2] or fallback]


def _tick(instrument: str, payload: Any) -> dict:
    raw = object_(payload)
    if raw.get("symbol") != instrument:
        raise VenueError("INVALID_RESPONSE")
    return {
        "instrument": instrument,
        "observed_at": number(raw.get("epoch")),
        "price": decimal(raw.get("quote")),
        "bid": optional_decimal(raw.get("bid")),
        "ask": optional_decimal(raw.get("ask")),
        "source": "DERIV_PUBLIC",
    }


def _contract_state(payload: Any, contract: int, account: dict) -> dict:
    row = object_(payload)
    if (
        row.get("contract_id") != contract
        or row.get("account_id") not in {None, account["account_id"]}
        or row.get("currency") != account["currency"]
    ):
        raise VenueError("INVALID_RESPONSE")
    sold = number(row.get("is_sold"))
    if sold not in {0, 1}:
        raise VenueError("INVALID_RESPONSE")
    settled_at = (
        number(row.get("sell_time", row.get("date_settlement"))) if sold else None
    )
    price = decimal(row.get("buy_price"))
    payout = decimal(row.get("sell_price") if sold else row.get("payout"))
    return {
        "broker_id": str(contract),
        "status": "SETTLED" if sold else "OPEN",
        "price": price,
        "payout": payout,
        "profit": decimal(row.get("profit")) if sold else None,
        "fee": optional_decimal(row.get("commission")),
        "settled_at": settled_at,
        "observed_at": int(time.time()),
        "currency": account["currency"],
        "source": "DERIV_DEMO",
    }

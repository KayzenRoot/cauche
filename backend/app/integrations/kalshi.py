"""Kalshi Prediction Markets Trade API V2. Public mode grants market reads only."""

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from typing import Any

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

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
    receive,
    request_json,
    socket_session,
    string,
)

_DEMO_REST = "https://external-api.demo.kalshi.co"
_PUBLIC_REST = "https://external-api.kalshi.com"
_DEMO_WS = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
_USER_WS = "wss://user-ws.demo.kalshi.co/trade-api/ws/v2"
_REST_PREFIX = "/trade-api/v2"
_WS_PATH = "/trade-api/ws/v2"
_ORDER_PATH = "/portfolio/events/orders"
_TICKER = r"[A-Z0-9][A-Z0-9_.-]{0,127}"
_CENTICENT = Decimal("0.0001")
_OPEN_STATUS = {"active", "open"}


class Kalshi:
    def __init__(
        self,
        key_id: str | None = None,
        private_key: str | None = None,
        *,
        public: bool = False,
        _transport: httpx.AsyncBaseTransport | None = None,
        _connect: Callable[..., Any] = NoRedirectConnect,
    ) -> None:
        self._key_id = key_id
        self._private_key = private_key
        self._signer: rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey | None = None
        self._public = public
        self._transport = _transport
        self._connect = _connect

    @property
    def source(self) -> str:
        return "KALSHI_PUBLIC" if self._public else "KALSHI_DEMO"

    def _require_demo(self) -> None:
        if self._public:
            raise VenueError("DEMO_DESTINATION_BLOCKED")
        if not self._key_id or not self._private_key:
            raise VenueError("NOT_CONFIGURED")

    def _headers(self, method: str, path: str) -> dict[str, str]:
        self._require_demo()
        key_id = identifier(self._key_id or "")
        if self._signer is None:
            if not isinstance(self._private_key, str):
                raise VenueError("AUTH_ERROR")
            try:
                pem = (self._private_key or "").encode("ascii")
                key = serialization.load_pem_private_key(pem, password=None)
            except (UnicodeError, ValueError, TypeError, UnsupportedAlgorithm):
                raise VenueError("AUTH_ERROR") from None
            if not isinstance(key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
                raise VenueError("AUTH_ERROR")
            self._signer = key
        timestamp = str(time.time_ns() // 1_000_000)
        message = (timestamp + method + path.partition("?")[0]).encode("utf-8")
        if isinstance(self._signer, rsa.RSAPrivateKey):
            signature = self._signer.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
        else:
            signature = self._signer.sign(message)
        return {
            "KALSHI-ACCESS-KEY": key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        }

    async def _read(
        self, path: str, *, params: dict | None = None, auth: bool = False
    ) -> dict:
        host = _PUBLIC_REST if self._public else _DEMO_REST
        full_path = _REST_PREFIX + path
        if auth:
            self._require_demo()
        balance_request = path == "/portfolio/balance"
        return await request_json(
            "GET",
            host + full_path,
            params=params,
            transport=self._transport,
            auth=(lambda: self._headers("GET", full_path)) if auth else None,
            pace_seconds=1 if balance_request else 0.2,
            pace_bucket="kalshi:portfolio-balance" if balance_request else None,
        )

    async def _write(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        self._require_demo()
        full_path = _REST_PREFIX + path
        return await request_json(
            method,
            _DEMO_REST + full_path,
            transport=self._transport,
            body=payload,
            params=params,
            auth=lambda: self._headers(method, full_path),
            mutation=True,
        )

    async def _pages(
        self, path: str, key: str, params: dict, *, auth: bool = False
    ) -> AsyncIterator[dict]:
        cursor, seen = "", set()
        for _ in range(50):
            page = await self._read(
                path,
                params={**params, **({"cursor": cursor} if cursor else {})},
                auth=auth,
            )
            # Authenticated inventories must explicitly certify the end of
            # pagination. Missing, null or non-string cursors are ambiguous.
            next_cursor = page.get("cursor")
            if next_cursor is None and not auth:
                next_cursor = ""
            if not isinstance(next_cursor, str) or next_cursor in seen:
                raise VenueError("PAGINATION_INCOMPLETE")
            if next_cursor and (
                len(next_cursor) > 4096 or not next_cursor.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in next_cursor)
            ):
                raise VenueError("PAGINATION_INCOMPLETE")
            rows = [object_(row) for row in array(page.get(key))]
            for row in rows:
                yield row
            if not next_cursor:
                return
            seen.add(next_cursor)
            cursor = next_cursor
        raise VenueError("PAGINATION_INCOMPLETE")

    async def markets(
        self, *, category: str | None = None, limit: int = 100
    ) -> list[dict]:
        if not 1 <= limit <= 500:
            raise VenueError("INVALID_LIMIT")
        rows = []
        params = {"status": "open", "limit": limit, "mve_filter": "exclude"}
        if category is None:
            async for market in self._pages("/markets", "markets", params):
                if market.get("market_type") == "binary":
                    rows.append(_market(market, self.source))
                    if len(rows) >= limit:
                        break
            return rows
        identifier(category, r"[A-Za-z0-9 &_-]{1,100}")
        reply = await self._read("/series", params={"category": category})
        for series in map(object_, array(reply.get("series"))):
            ticker = identifier(string(series.get("ticker")), _TICKER)
            async for market in self._pages(
                "/markets",
                "markets",
                {**params, "series_ticker": ticker},
            ):
                if market.get("market_type") == "binary":
                    rows.append(
                        {
                            **_market(market, self.source),
                            "category": series.get("category"),
                            "series_ticker": ticker,
                        }
                    )
                    if len(rows) >= limit:
                        return rows
        return rows

    async def market(self, instrument: str, *, include_fees: bool = True) -> dict:
        ticker = identifier(instrument, _TICKER)
        payload = await self._read(f"/markets/{ticker}")
        raw = object_(payload.get("market"))
        result = _market(raw, self.source)
        if result["instrument"] != ticker:
            raise VenueError("INVALID_RESPONSE")
        if not include_fees:
            return result
        event_ticker = result["event_ticker"]
        event_response = await self._read(f"/events/{event_ticker}")
        event = object_(event_response.get("event"))
        if event.get("event_ticker") != event_ticker:
            raise VenueError("INVALID_RESPONSE")
        series_ticker = identifier(string(event.get("series_ticker")), _TICKER)
        series_response = await self._read(f"/series/{series_ticker}")
        series = object_(series_response.get("series"))
        if series.get("ticker") != series_ticker:
            raise VenueError("INVALID_RESPONSE")
        maker, taker = _fee_rates(event, series)
        return {
            **result,
            "category": series.get("category"),
            "series_ticker": series_ticker,
            "mutually_exclusive": event.get("mutually_exclusive") is True,
            "maker_fee_rate": maker,
            "taker_fee_rate": taker,
        }

    async def related_markets(self, instrument: str, *, limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 500:
            raise VenueError("INVALID_LIMIT")
        source = await self.market(instrument, include_fees=False)
        rows = []
        async for market in self._pages(
            "/markets",
            "markets",
            {"event_ticker": source["event_ticker"], "limit": limit},
        ):
            if market.get("market_type") == "binary":
                rows.append(_market(market, self.source))
                if len(rows) >= limit:
                    break
        return rows

    async def exclusive_siblings(self, market: dict) -> list[dict]:
        """Use exchange-certified, same-event exclusivity with matched settlement terms."""
        ticker = identifier(market["event_ticker"], _TICKER)
        payload = await self._read(
            f"/events/{ticker}", params={"with_nested_markets": "true"},
        )
        event = object_(payload.get("event"))
        if (
            event.get("event_ticker") != ticker
            or event.get("mutually_exclusive") is not True
            or event.get("collateral_return_type") != "MECNET"
            or not event.get("settlement_sources")
        ):
            return []
        raw_markets = array(event.get("markets", []))
        if len(raw_markets) > 100:
            return []
        available = []
        for row in map(object_, raw_markets):
            normalized = _market(row, self.source)
            if (
                normalized["instrument"] != market["instrument"]
                and normalized["event_ticker"] == ticker
                and normalized["market_type"] == market["market_type"] == "binary"
                and normalized["status"] == market["status"] == "active"
                and normalized["close_time"] == market["close_time"]
                and normalized["notional"] == market["notional"] == Decimal("1")
                and isinstance(market["settlement_timer"], int)
                and normalized["settlement_timer"] == market["settlement_timer"]
                and normalized["rules_primary"] and market["rules_primary"]
                and normalized["rules_secondary"] == market["rules_secondary"]
            ):
                available.append(normalized)
        return available

    async def candles(
        self, instrument: str, interval_seconds: int = 300, limit: int = 200
    ) -> list[dict]:
        ticker = identifier(instrument, _TICKER)
        if interval_seconds not in {60, 300, 3600, 86400} or not 1 <= limit <= 1000:
            raise VenueError("INVALID_HISTORY_RANGE")
        end = int(time.time()) // interval_seconds * interval_seconds
        start = end - interval_seconds * limit
        # Use Kalshi's documented native 1m, 1h and 1d intervals.
        native_interval = 60 if interval_seconds == 300 else interval_seconds
        response = await self._read(
            "/markets/candlesticks",
            params={
                "market_tickers": ticker,
                "period_interval": native_interval // 60,
                "start_ts": start,
                "end_ts": end,
                "include_latest_before_start": "false",
            },
        )
        records = list(map(object_, array(response.get("markets"))))
        if len(records) != 1 or records[0].get("market_ticker") != ticker:
            raise VenueError("INVALID_RESPONSE")
        result = []
        for raw in map(object_, array(records[0].get("candlesticks"))):
            closed = number(raw.get("end_period_ts"))
            if not start < closed <= end:
                continue
            stats = object_(raw.get("price"))
            if any(
                stats.get(item + "_dollars") is None
                for item in ("open", "high", "low", "close")
            ):
                continue
            result.append(
                bar(
                    closed - native_interval,
                    stats,
                    suffix="_dollars",
                    volume=decimal(raw.get("volume_fp")),
                )
            )
        result.sort(key=lambda row: row["time"])
        if any(a["time"] == b["time"] for a, b in zip(result, result[1:])):
            raise VenueError("INVALID_RESPONSE")
        if interval_seconds != native_interval:
            result = _aggregate_bars(result, interval_seconds)
        return result[-limit:]

    async def orderbook(
        self, instrument: str, *, depth: int = 20, market: dict | None = None
    ) -> dict:
        ticker = identifier(instrument, _TICKER)
        if not 1 <= depth <= 100:
            raise VenueError("INVALID_DEPTH")
        if market is None:
            market = await self.market(ticker, include_fees=False)
        if market.get("instrument") != ticker:
            raise VenueError("INVALID_RESPONSE")
        observed = int(time.time())
        raw = await self._read(f"/markets/{ticker}/orderbook", params={"depth": depth})
        book = object_(raw.get("orderbook_fp"))
        return {
            **_book(_levels(book.get("yes_dollars")), _levels(book.get("no_dollars"))),
            "instrument": ticker,
            "event": market["event_ticker"],
            "observed_at": observed,
            "source": self.source,
        }

    async def verify_demo(self) -> dict:
        row = await self._read("/portfolio/balance", auth=True)
        fixed_balance = row.get("balance_dollars")
        balance = (
            decimal(fixed_balance)
            if fixed_balance is not None
            else Decimal(number(row.get("balance"))) / 100
        )
        if balance < 0:
            raise VenueError("INVALID_RESPONSE")
        value = Decimal(number(row.get("portfolio_value"))) / 100
        return {
            "account_id": None,
            "currency": "USD",
            "balance": balance,
            "portfolio_value": value,
            "environment": "BROKER_DEMO",
            "source": "KALSHI_DEMO",
            "observed_at": int(time.time()),
            "updated_at": number(row["updated_ts"])
            if row.get("updated_ts") is not None
            else None,
            "capabilities": ["BALANCE_READ"],
        }

    async def submit_demo(
        self,
        instrument: str,
        side: str,
        price: Decimal,
        quantity: Decimal,
        *,
        client_order_id: str,
        post_only: bool = False,
    ) -> dict:
        self._require_demo()
        ticker = identifier(instrument, _TICKER)
        client_order_id = identifier(client_order_id, r"[A-Za-z0-9_:+/=-]{1,64}")
        price, quantity = decimal(price), decimal(quantity)
        if side not in {"YES", "NO"} or not 0 < price < 1 or not 0 < quantity <= 10000:
            raise VenueError("INVALID_ORDER")
        if price != price.quantize(_CENTICENT) or quantity != quantity.quantize(
            Decimal("0.01")
        ):
            raise VenueError("INVALID_PRECISION")
        wire_price = price if side == "YES" else 1 - price
        market = await self.market(ticker, include_fees=False)
        if (
            market["market_type"] != "binary"
            or market["status"] not in _OPEN_STATUS
            or market["close_time"] <= int(time.time())
        ):
            raise VenueError("MARKET_NOT_TRADABLE")
        if not any(
            item["start"] <= wire_price <= item["end"]
            and (wire_price - item["start"]) % item["step"] == 0
            for item in market["price_ranges"]
        ):
            raise VenueError("PRICE_GRID_MISMATCH")
        payload = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": "bid" if side == "YES" else "ask",
            "count": f"{quantity:.2f}",
            "price": f"{wire_price:.4f}",
            "time_in_force": "good_till_canceled"
            if post_only
            else "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": post_only,
            "cancel_order_on_pause": True,
            "exchange_index": -1,
        }
        if post_only:
            payload["expiration_time"] = min(
                market["close_time"], int(time.time()) + 45
            )
        raw = await self._write("POST", _ORDER_PATH, payload=payload)
        try:
            filled = _quantity(raw.get("fill_count"))
            remaining = _quantity(raw.get("remaining_count"))
            if (
                raw.get("client_order_id") not in {None, client_order_id}
                or filled + remaining > quantity
                or (remaining and not post_only)
            ):
                raise VenueError("INVALID_RESPONSE")
            fee = Decimal(0)
            average_fill_price = None
            if filled:
                average = _price(raw.get("average_fill_price"))
                average_fill_price = average if side == "YES" else 1 - average
                fee = decimal(raw.get("average_fee_paid")) * filled
                if average_fill_price > price or fee < 0:
                    raise VenueError("BROKER_QUOTE_DIVERGENCE")
            return {
                "broker_id": identifier(string(raw.get("order_id"))),
                "client_order_id": client_order_id,
                "instrument": ticker,
                "side": side,
                "status": _order_status(filled, remaining, quantity),
                "price": price,
                "average_fill_price": average_fill_price,
                "quantity": quantity,
                "filled_quantity": filled,
                "remaining_quantity": remaining,
                "fee": fee,
                "observed_at": number(raw.get("ts_ms")) // 1000,
                "source": "KALSHI_DEMO",
            }
        except VenueError as exc:
            raise VenueError(exc.code, outcome_unknown=True) from None

    async def cancel_demo(self, broker_id: str, instrument: str) -> dict:
        oid = identifier(broker_id)
        ticker = identifier(instrument, _TICKER)
        raw = await self._write(
            "DELETE",
            f"{_ORDER_PATH}/{oid}",
            params={"market_ticker": ticker, "exchange_index": -1},
        )
        try:
            if raw.get("order_id") != oid:
                raise VenueError("INVALID_RESPONSE")
            return {
                "broker_id": oid,
                "reduced_quantity": _quantity(raw.get("reduced_by")),
                "observed_at": number(raw.get("ts_ms")) // 1000,
            }
        except VenueError as exc:
            raise VenueError(exc.code, outcome_unknown=True) from None

    async def find_demo_order(
        self, client_order_id: str, instrument: str
    ) -> dict | None:
        c_oid = identifier(client_order_id, r"[A-Za-z0-9_:+/=-]{1,64}")
        ticker = identifier(instrument, _TICKER)
        matches = []
        async for raw in self._pages(
            "/portfolio/orders",
            "orders",
            {"ticker": ticker, "limit": 200},
            auth=True,
        ):
            if raw.get("client_order_id") == c_oid and raw.get("ticker") == ticker:
                matches.append(_normalized_order(raw))
        if len(matches) > 1:
            raise VenueError("ORDER_CONFLICT", outcome_unknown=True)
        return matches[0] if matches else None

    async def open_orders(self) -> list[dict]:
        """List resting demo orders without weakening the bounded reconciliation reads."""
        orders = [
            _normalized_order(row)
            async for row in self._pages(
                "/portfolio/orders",
                "orders",
                {"status": "resting", "limit": 200},
                auth=True,
            )
        ]
        if any(
            order["broker_status"] != "resting" or order["remaining_quantity"] <= 0
            for order in orders
        ):
            raise VenueError("INVALID_RESPONSE")
        return orders

    async def reconcile_demo(self, broker_id: str, instrument: str) -> dict:
        oid, ticker = identifier(broker_id), identifier(instrument, _TICKER)
        payload = await self._read(f"/portfolio/orders/{oid}", auth=True)
        order = _normalized_order(object_(payload.get("order")))
        if order["instrument"] != ticker or order["broker_id"] != oid:
            raise VenueError("INVALID_RESPONSE")
        fills = []
        seen = set()
        async for row in self._pages(
            "/portfolio/fills", "fills", {"order_id": oid, "limit": 200}, auth=True
        ):
            fill = _fill(row)
            if (
                fill["broker_id"] != oid
                or fill["instrument"] != ticker
                or fill["fill_id"] in seen
            ):
                raise VenueError("INVALID_RESPONSE")
            fills.append(fill)
            seen.add(fill["fill_id"])
        settled = await self.settlements(instrument)
        return {"order": order, "fills": fills, "settlements": settled}

    async def positions(self, instrument: str | None = None) -> list[dict]:
        params: dict = {"limit": 200}
        if instrument is not None:
            params["ticker"] = identifier(instrument, _TICKER)
        return [
            {
                "instrument": identifier(string(row.get("ticker")), _TICKER),
                "quantity": decimal(row.get("position_fp")),
                "exposure": decimal(row.get("market_exposure_dollars")),
                "realized_pnl": decimal(row.get("realized_pnl_dollars")),
                "fee": decimal(row.get("fees_paid_dollars")),
                "observed_at": _optional_timestamp(row.get("last_updated_ts")),
            }
            async for row in self._pages(
                "/portfolio/positions", "market_positions", params, auth=True
            )
        ]

    async def settlements(self, instrument: str) -> list[dict]:
        ticker = identifier(instrument, _TICKER)
        return [
            _settlement(row)
            async for row in self._pages(
                "/portfolio/settlements",
                "settlements",
                {"ticker": ticker, "limit": 200},
                auth=True,
            )
            if row.get("ticker") == ticker
        ]

    async def stream_ticker(self, instrument: str) -> AsyncIterator[dict]:
        ticker = identifier(instrument, _TICKER)
        async with aclosing(self._stream(_DEMO_WS, "ticker", ticker)) as stream:
            async for message in stream:
                if "status" in message:
                    yield message
                    continue
                if message.get("type") != "ticker":
                    continue
                row = object_(message.get("msg"))
                if row.get("market_ticker") != ticker:
                    raise VenueError("INVALID_RESPONSE")
                yield {
                    "instrument": ticker,
                    "price": _price(row.get("price_dollars")),
                    "yes_bid": _price(row.get("yes_bid_dollars")),
                    "yes_ask": _price(row.get("yes_ask_dollars")),
                    "yes_bid_size": _quantity(row.get("yes_bid_size_fp")),
                    "yes_ask_size": _quantity(row.get("yes_ask_size_fp")),
                    "observed_at": number(row.get("ts_ms")) // 1000,
                    "source": "KALSHI_DEMO",
                }

    async def stream_user_fills(self, instrument: str) -> AsyncIterator[dict]:
        ticker = identifier(instrument, _TICKER)
        async with aclosing(self._stream(_USER_WS, "fill", ticker)) as stream:
            async for message in stream:
                if "status" in message:
                    yield message
                elif message.get("type") == "fill":
                    fill = _fill(object_(message.get("msg")))
                    if fill["instrument"] == ticker:
                        yield fill

    async def stream_orderbook(self, instrument: str) -> AsyncIterator[dict]:
        ticker = identifier(instrument, _TICKER)
        while True:
            book: dict[str, dict[Decimal, Decimal]] = {}
            sequence = 0
            async with aclosing(
                self._stream(_DEMO_WS, "orderbook_delta", ticker)
            ) as stream:
                async for message in stream:
                    if "status" in message:
                        book.clear()
                        sequence = 0
                        yield message
                        continue
                    kind = message.get("type")
                    if kind not in {"orderbook_snapshot", "orderbook_delta"}:
                        continue
                    row = object_(message.get("msg"))
                    if row.get("market_ticker") != ticker:
                        raise VenueError("INVALID_RESPONSE")
                    next_sequence = number(message.get("seq"))
                    if kind == "orderbook_snapshot":
                        book = {
                            "yes": _levels(row.get("yes_dollars_fp", [])),
                            "no": _levels(row.get("no_dollars_fp", [])),
                        }
                    elif book and next_sequence == sequence + 1:
                        _apply_delta(book, row)
                    else:
                        yield {
                            "status": "BOOK_SEQUENCE_GAP",
                            "instrument": ticker,
                            "observed_at": int(time.time()),
                        }
                        break
                    sequence = next_sequence
                    received = int(time.time() * 1000)
                    observed = row.get("ts_ms", message.get("sending_ts_ms", received))
                    yield {
                        **_book(book["yes"], book["no"]),
                        "instrument": ticker,
                        "source": "KALSHI_DEMO",
                        "observed_at": number(observed) // 1000,
                    }
            await asyncio.sleep(1)

    async def _stream(self, url: str, channel: str, ticker: str) -> AsyncIterator[dict]:
        delay = 1
        subscription = {
            "id": 1,
            "cmd": "subscribe",
            "params": {"channels": [channel], "market_tickers": [ticker]},
        }
        while True:
            try:
                async with socket_session(
                    url, self._connect, self._headers("GET", _WS_PATH)
                ) as websocket:
                    async with asyncio.timeout(8):
                        await websocket.send(json.dumps(subscription))
                    while True:
                        message = await receive(websocket)
                        if message.get("type") == "error":
                            raise VenueError("SUBSCRIPTION_REJECTED")
                        delay = 1
                        yield message
            except VenueError as exc:
                if not exc.retryable:
                    raise
                yield {
                    "status": exc.code,
                    "instrument": ticker,
                    "observed_at": int(time.time()),
                }
                await asyncio.sleep(max(delay, exc.retry_after or 0))
                delay = min(delay * 2, 30)


def _aggregate_bars(rows: list[dict], interval: int) -> list[dict]:
    """Group only observed trade prices and volumes; empty buckets stay empty."""
    grouped = {}
    for row in rows:
        opened = row["time"] // interval * interval
        previous = grouped.get(opened)
        if previous is None:
            grouped[opened] = {**row, "time": opened}
        else:
            previous["high"] = max(previous["high"], row["high"])
            previous["low"] = min(previous["low"], row["low"])
            previous["close"] = row["close"]
            previous["volume"] += row["volume"]
    return list(grouped.values())


def _market(raw: dict, source: str) -> dict:
    outcome = raw.get("result")
    return {
        "instrument": identifier(string(raw.get("ticker")), _TICKER),
        "event_ticker": identifier(string(raw.get("event_ticker")), _TICKER),
        "series_ticker": None,
        "name": raw.get("title") or raw.get("yes_sub_title") or raw["ticker"],
        "status": string(raw.get("status")),
        "market_type": string(raw.get("market_type")),
        "notional": optional_decimal(raw.get("notional_value_dollars")),
        "settlement_timer": raw.get("settlement_timer_seconds"),
        "result": outcome.upper() if outcome in {"yes", "no", "scalar"} else None,
        "close_time": _timestamp(raw.get("close_time")),
        "settled_at": _optional_timestamp(raw.get("settlement_ts")),
        "yes_settlement_value": optional_decimal(raw.get("settlement_value_dollars")),
        "rules_primary": raw.get("rules_primary"),
        "rules_secondary": raw.get("rules_secondary"),
        "price_ranges": [
            _price_range(item) for item in array(raw.get("price_ranges", []))
        ],
        "maker_fee_rate": None,
        "taker_fee_rate": None,
        "yes_bid": optional_decimal(raw.get("yes_bid_dollars")),
        "yes_ask": optional_decimal(raw.get("yes_ask_dollars")),
        "no_bid": optional_decimal(raw.get("no_bid_dollars")),
        "no_ask": optional_decimal(raw.get("no_ask_dollars")),
        "volume": optional_decimal(raw.get("volume_fp")),
        "source": source,
    }


def _fee_rates(event: dict, series: dict) -> tuple[Decimal | None, Decimal | None]:
    fee_type = event.get("fee_type_override") or series.get("fee_type")
    multiplier = event.get("fee_multiplier_override")
    if multiplier is None:
        multiplier = series.get("fee_multiplier")
    scales = {
        "quadratic": Decimal(0),
        "quadratic_with_maker_fees": Decimal("0.25"),
        "quadratic_with_combo_maker_fees": Decimal("0.50"),
    }
    if fee_type not in scales or multiplier is None:
        return None, None
    amount = decimal(multiplier)
    if amount < 0:
        raise VenueError("INVALID_RESPONSE")
    taker = amount * Decimal("0.07")
    return taker * scales[fee_type], taker


def fee_estimate(price: Decimal, quantity: Decimal, rate: Decimal) -> Decimal:
    """Estimate a quadratic fee rounding fee + cost up to the next centicent."""
    cost = price * quantity
    trade_fee = (rate * quantity * price * (1 - price)).quantize(
        Decimal("0.000001"),
        rounding=ROUND_CEILING,
    )
    return (cost + trade_fee).quantize(
        _CENTICENT,
        rounding=ROUND_CEILING,
    ) - cost


def _price_range(raw: Any) -> dict:
    row = object_(raw)
    start, end, step = (
        _price(row.get("start")),
        _price(row.get("end")),
        _price(row.get("step")),
    )
    if not 0 < step <= end - start:
        raise VenueError("INVALID_RESPONSE")
    return {"start": start, "end": end, "step": step}


def _timestamp(value: Any) -> int:
    try:
        date = datetime.fromisoformat(string(value))
        if date.tzinfo is not None:
            return int(date.timestamp())
    except (ValueError, OverflowError, OSError):
        pass
    raise VenueError("INVALID_RESPONSE")


def _optional_timestamp(value: Any) -> int | None:
    return None if value is None else _timestamp(value)


def _price(value: Any) -> Decimal:
    price = decimal(value)
    if not 0 <= price <= 1:
        raise VenueError("INVALID_RESPONSE")
    return price


def _quantity(value: Any) -> Decimal:
    quantity = decimal(value)
    if quantity < 0 or quantity != quantity.quantize(Decimal("0.01")):
        raise VenueError("INVALID_RESPONSE")
    return quantity


def _levels(raw: Any) -> dict[Decimal, Decimal]:
    levels = {}
    for record in array(raw):
        pair = array(record)
        if len(pair) != 2:
            raise VenueError("INVALID_RESPONSE")
        price, size = _price(pair[0]), _quantity(pair[1])
        if price in levels or size <= 0:
            raise VenueError("INVALID_RESPONSE")
        levels[price] = size
    return levels


def _book(yes: dict[Decimal, Decimal], no: dict[Decimal, Decimal]) -> dict:
    yes_bid, no_bid = max(yes, default=None), max(no, default=None)
    yes_ask, no_ask = (
        (None if no_bid is None else 1 - no_bid),
        (None if yes_bid is None else 1 - yes_bid),
    )
    yes_size, no_size = yes.get(yes_bid, Decimal(0)), no.get(no_bid, Decimal(0))
    if yes_bid is not None and yes_ask is not None and yes_bid > yes_ask:
        raise VenueError("CROSSED_BOOK", retryable=True)
    return {
        "yes_bid": yes_bid,
        "no_bid": no_bid,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "yes_bid_size": yes_size,
        "no_bid_size": no_size,
        "yes_ask_size": no_size,
        "no_ask_size": yes_size,
        "yes_bids": sorted(yes.items(), reverse=True),
        "no_bids": sorted(no.items(), reverse=True),
    }


def _apply_delta(book: dict[str, dict], row: dict) -> None:
    side = row.get("side")
    if side not in book:
        raise VenueError("INVALID_RESPONSE")
    price = _price(row.get("price_dollars"))
    total = _quantity(book[side].get(price, 0) + decimal(row.get("delta_fp")))
    if total:
        book[side][price] = total
    else:
        book[side].pop(price, None)


def _order_status(filled: Decimal, remaining: Decimal, quantity: Decimal) -> str:
    if filled == quantity:
        return "FILLED"
    if filled > 0:
        return "PARTIAL"
    return "OPEN" if remaining > 0 else "CANCELLED"


def _normalized_order(row: dict) -> dict:
    side = string(row.get("outcome_side")).upper()
    if side not in {"YES", "NO"}:
        raise VenueError("INVALID_RESPONSE")
    if row.get("book_side") != ("bid" if side == "YES" else "ask"):
        raise VenueError("INVALID_RESPONSE")
    filled = _quantity(row.get("fill_count_fp"))
    remaining = _quantity(row.get("remaining_count_fp"))
    quantity = _quantity(row.get("initial_count_fp"))
    status = row.get("status")
    if (
        quantity <= 0
        or filled + remaining > quantity
        or status not in {"resting", "canceled", "executed"}
    ):
        raise VenueError("INVALID_RESPONSE")
    fee = decimal(row.get("maker_fees_dollars")) + decimal(
        row.get("taker_fees_dollars")
    )
    return {
        "broker_id": identifier(string(row.get("order_id"))),
        "client_order_id": row.get("client_order_id"),
        "instrument": identifier(string(row.get("ticker")), _TICKER),
        "side": side,
        "status": _order_status(filled, remaining, quantity),
        "broker_status": status,
        "quantity": quantity,
        "filled_quantity": filled,
        "remaining_quantity": remaining,
        "price": _price(row.get(f"{side.lower()}_price_dollars")),
        "fee": fee,
        "observed_at": _optional_timestamp(
            row.get("last_update_time") or row.get("created_time")
        ),
        "source": "KALSHI_DEMO",
    }


def _fill(row: dict) -> dict:
    side = string(row.get("outcome_side")).upper()
    if side not in {"YES", "NO"}:
        raise VenueError("INVALID_RESPONSE")
    when = row.get("created_time")
    if when is not None:
        observed = _timestamp(when)
    elif row.get("ts_ms") is not None:
        observed = number(row.get("ts_ms")) // 1000
    else:
        observed = number(row.get("ts"))
    yes_price = _price(row.get("yes_price_dollars"))
    no_price = row.get("no_price_dollars")
    if no_price is not None and _price(no_price) != 1 - yes_price:
        raise VenueError("INVALID_RESPONSE")
    quantity = _quantity(row.get("count_fp"))
    fee = decimal(row.get("fee_cost"))
    if quantity <= 0 or fee < 0 or not isinstance(row.get("is_taker"), bool):
        raise VenueError("INVALID_RESPONSE")
    return {
        "fill_id": identifier(string(row.get("fill_id") or row.get("trade_id"))),
        "broker_id": identifier(string(row.get("order_id"))),
        "instrument": identifier(
            string(row.get("market_ticker") or row.get("ticker")), _TICKER
        ),
        "side": side,
        "quantity": quantity,
        "price": yes_price if side == "YES" else 1 - yes_price,
        "fee": fee,
        "is_taker": row.get("is_taker") is True,
        "observed_at": observed,
    }


def _settlement(row: dict) -> dict:
    ticker = identifier(string(row.get("ticker")), _TICKER)
    event = identifier(string(row.get("event_ticker")), _TICKER)
    result = string(row.get("market_result")).upper()
    when = _timestamp(row.get("settled_time"))
    value = row.get("value")
    if value is not None:
        payout = _price(Decimal(number(value)) / 100)
    elif result in {"YES", "NO"}:
        payout = Decimal(1 if result == "YES" else 0)
    else:
        raise VenueError("INVALID_RESPONSE")
    return {
        "settlement_id": f"{ticker}:{when}",
        "instrument": ticker,
        "event_ticker": event,
        "result": result,
        "yes_payout": payout,
        "yes_quantity": _quantity(row.get("yes_count_fp")),
        "no_quantity": _quantity(row.get("no_count_fp")),
        "revenue": Decimal(number(row.get("revenue"))) / 100,
        "cost": decimal(row.get("yes_total_cost_dollars"))
        + decimal(row.get("no_total_cost_dollars")),
        "fee": decimal(row.get("fee_cost")),
        "observed_at": when,
    }

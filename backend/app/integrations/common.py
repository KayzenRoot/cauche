"""Transport primitives that keep upstream credentials and errors out of application logs."""

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal, DecimalException
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException


class VenueError(Exception):
    """Sanitized adapter failure suitable for a persisted event."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        outcome_unknown: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        self.retry_after = retry_after


class NoRedirectConnect(connect):
    """Never replay API key headers or a demo OTP across a redirect."""

    def process_redirect(self, exc: Exception) -> Exception:
        return exc


_socket_logger = logging.Logger("cauche.venue.websocket", level=logging.CRITICAL + 1)
_socket_logger.addHandler(logging.NullHandler())
_http_limiter_lock = threading.Lock()
_next_request: dict[str, float] = {}


async def pace(bucket: str, interval: float = 0.2) -> None:
    """Conservative process-wide pacing; the server's limits still apply."""
    now = time.monotonic()
    with _http_limiter_lock:
        reserved = max(now, _next_request.get(bucket, 0))
        _next_request[bucket] = reserved + interval
    if reserved > now:
        await asyncio.sleep(reserved - now)


def identifier(value: str, pattern: str = r"[A-Za-z0-9_-]{1,128}") -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise VenueError("INVALID_IDENTIFIER")
    return value


def decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise VenueError("INVALID_RESPONSE")
    try:
        result = Decimal(str(value))
        if (
            not result.is_finite()
            or result.adjusted() > 14
            or result.as_tuple().exponent < -16
        ):
            raise VenueError("INVALID_RESPONSE")
    except (ValueError, DecimalException):
        raise VenueError("INVALID_RESPONSE") from None
    return result


def number(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise VenueError("INVALID_RESPONSE")
    return value


def string(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise VenueError("INVALID_RESPONSE")
    return value


def optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else decimal(value)


def object_(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VenueError("INVALID_RESPONSE")
    return value


def array(value: Any) -> list:
    if not isinstance(value, list):
        raise VenueError("INVALID_RESPONSE")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise ValueError("Non-finite JSON constant")


def parse_json(raw: str | bytes) -> dict[str, Any]:
    try:
        return object_(json.loads(
            raw, parse_float=Decimal, object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        ))
    except (ValueError, TypeError, DecimalException, UnicodeError, RecursionError):
        raise VenueError("INVALID_RESPONSE") from None


def _delay(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After", "")
    return (
        min(60, int(header))
        if header.isascii() and header.isdecimal() and len(header) < 4
        else None
    )


def status_error(
    status: int,
    *,
    mutation: bool = False,
    retry_after: float | None = None,
) -> VenueError:
    if status in {401, 403}:
        return VenueError("AUTH_ERROR")
    if status == 429:
        return VenueError("RATE_LIMIT", retryable=not mutation, retry_after=retry_after)
    if status == 404:
        return VenueError("NOT_FOUND")
    if 300 <= status < 400:
        return VenueError("REDIRECT_BLOCKED", outcome_unknown=mutation)
    if status in {408, 425} or status >= 500:
        return VenueError(
            "UPSTREAM_UNAVAILABLE", retryable=not mutation, outcome_unknown=mutation
        )
    if status == 409 and mutation:
        return VenueError("ORDER_CONFLICT", outcome_unknown=True)
    return VenueError("UPSTREAM_REJECTED")


async def request_json(
    method: str,
    url: str,
    *,
    auth: Callable[[], dict[str, str]] | None = None,
    params: dict | None = None,
    body: dict | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    mutation: bool = False,
    pace_seconds: float = 0.2,
    pace_bucket: str | None = None,
) -> dict:
    attempts = 3 if method == "GET" and not mutation else 1
    async with httpx.AsyncClient(
        timeout=8,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        attempt = 0
        while True:
            try:
                await pace(pace_bucket or httpx.URL(url).host, pace_seconds)
                response = await client.request(
                    method,
                    url,
                    headers=auth() if auth else None,
                    params=params,
                    json=body,
                )
                if not response.is_success:
                    raise status_error(
                        response.status_code,
                        mutation=mutation,
                        retry_after=_delay(response),
                    )
                try:
                    return parse_json(response.content)
                except VenueError as exc:
                    raise VenueError(exc.code, outcome_unknown=mutation) from None
            except (httpx.HTTPError, TimeoutError):
                error = VenueError(
                    "DISCONNECTED", retryable=not mutation, outcome_unknown=mutation
                )
            except VenueError as exc:
                error = exc
            delay = (
                error.retry_after if error.retry_after is not None else 0.5 * 2**attempt
            )
            if attempt + 1 >= attempts or not error.retryable or delay > 5:
                raise error from None
            await asyncio.sleep(delay)
            attempt += 1


@asynccontextmanager
async def socket_session(
    url: str,
    factory: Callable[..., Any],
    headers: dict[str, str] | None = None,
) -> AsyncIterator[Any]:
    try:
        await pace(httpx.URL(url).host)
        async with factory(
            url,
            additional_headers=headers,
            open_timeout=8,
            close_timeout=2,
            ping_interval=20,
            ping_timeout=10,
            proxy=None,
            logger=_socket_logger,
        ) as websocket:
            yield websocket
    except InvalidStatus as exc:
        raise status_error(exc.response.status_code) from None
    except (WebSocketException, OSError, TimeoutError):
        raise VenueError("DISCONNECTED", retryable=True) from None


async def receive(websocket: Any, timeout: float = 25) -> dict:
    async with asyncio.timeout(timeout):
        return parse_json(await websocket.recv())


def bar(
    time_: int, raw: dict, *, suffix: str = "", volume: Decimal | None = None
) -> dict:
    result = {
        key: decimal(raw.get(key + suffix)) for key in ("open", "high", "low", "close")
    }
    if (
        time_ < 0
        or result["low"] <= 0
        or not result["low"]
        <= min(result["open"], result["close"])
        <= max(result["open"], result["close"])
        <= result["high"]
        or (volume is not None and volume < 0)
    ):
        raise VenueError("INVALID_RESPONSE")
    return {"time": time_, **result, "volume": volume}

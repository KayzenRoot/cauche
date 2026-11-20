"""Contract tests for fixed, non-executing account verification endpoints."""

import base64
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.integrations import VenueError
from app.integrations.real_accounts import DerivRealReader, KalshiRealReader


@pytest.mark.asyncio
async def test_deriv_real_reader_uses_only_account_list_and_validates_type():
    calls = []

    def respond(request):
        calls.append((request.method, str(request.url)))
        assert request.headers["deriv-app-id"] == "test-app"
        return httpx.Response(200, json={"data": [
            {"account_id": "DOT-DEMO", "account_type": "demo", "status": "active", "currency": "USD", "balance": 500},
            {"account_id": "DOT-REAL", "account_type": "real", "status": "active", "currency": "USD", "balance": 105.25},
        ]})

    transport = httpx.MockTransport(respond)
    assert await DerivRealReader("test-app", "test-credential", "DOT-REAL", _transport=transport).verify() == (
        "USD", Decimal("105.25"),
    )
    with pytest.raises(VenueError, match="REAL_ACCOUNT_REQUIRED"):
        await DerivRealReader("test-app", "test-credential", "DOT-DEMO", _transport=transport).verify()
    assert calls == [("GET", "https://api.derivws.com/trading/v1/options/accounts")] * 2


@pytest.mark.asyncio
async def test_kalshi_real_reader_uses_signed_get_and_blocks_redirect():
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url) == "https://external-api.kalshi.com/trade-api/v2/portfolio/balance"
        assert request.method == "GET" and request.headers["KALSHI-ACCESS-KEY"] == "fixture-key"
        signature = base64.b64decode(request.headers["KALSHI-ACCESS-SIGNATURE"])
        timestamp = request.headers["KALSHI-ACCESS-TIMESTAMP"]
        key.public_key().verify(signature, (timestamp + "GET" + request.url.path).encode())
        if len(requests) > 1:
            return httpx.Response(307, headers={"Location": "https://example.test/redirect"})
        return httpx.Response(200, json={"balance_dollars": "123.450001"})

    reader = KalshiRealReader("fixture-key", pem, _transport=httpx.MockTransport(respond))
    assert await reader.verify() == ("USD", Decimal("123.450001"))
    with pytest.raises(VenueError, match="REDIRECT_BLOCKED"):
        await reader.verify()
    assert len(requests) == 2

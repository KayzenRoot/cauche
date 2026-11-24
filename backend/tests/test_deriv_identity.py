"""CD-19: demo account identity and OTP destination, isolated transport only."""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.integrations import Deriv, VenueError
from app.integrations import common, deriv

ACCOUNT = "DOT90000000"
OTHER = "DOT90000001"
ACCOUNT_PATH = "/trading/v1/options/accounts"
OTP_URL = "wss://api.derivws.com/trading/v1/options/ws/demo?otp=fixture-only"


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(common, "pace", AsyncMock())
    monkeypatch.setattr(deriv, "pace", AsyncMock())


def account(identity=ACCOUNT, **overrides):
    return {"account_id": identity, "account_type": "demo", "status": "active", "currency": "USD", **overrides}


def adapter(accounts, *, selected=None, otp=OTP_URL):
    calls = []
    socket = Mock(side_effect=AssertionError("socket must not open in account discovery test"))

    def respond(request):
        calls.append((request.method, request.url.path))
        assert request.url.host == "api.derivws.com"
        assert request.headers["Deriv-App-ID"] == "fixture-app"
        assert request.headers["Authorization"] == "Bearer fixture-token"
        if request.method == "GET":
            assert request.url.path == ACCOUNT_PATH
            return httpx.Response(200, json={"data": accounts})
        assert request.method == "POST"
        assert request.url.path == f"{ACCOUNT_PATH}/{ACCOUNT}/otp"
        return httpx.Response(200, json={"data": {"url": otp}})

    instance = Deriv(
        "fixture-app", "fixture-token", selected,
        _transport=httpx.MockTransport(respond), _connect=socket,
    )
    return instance, calls, socket


@pytest.mark.parametrize("conflict", [
    account(account_type="real"), account(status="inactive"), account(currency="EUR"),
    account(account_type="real", status="inactive"),
])
@pytest.mark.parametrize("explicit", [False, True])
async def test_conflicting_account_identity_never_requests_otp(conflict, explicit):
    instance, calls, socket = adapter([account(), conflict], selected=ACCOUNT if explicit else None)
    with pytest.raises(VenueError, match="^DEMO_ACCOUNT_REQUIRED$"):
        await instance.verify_demo()
    assert calls == [("GET", ACCOUNT_PATH)]
    socket.assert_not_called()


@pytest.mark.parametrize("rows,selected", [
    ([], None), ([account(OTHER)], ACCOUNT),
    ([account(), account(OTHER)], None),
    ([account(), account()], ACCOUNT),
    ([account(account_type="real")], ACCOUNT),
    ([account(status="inactive")], ACCOUNT),
])
async def test_no_unique_eligible_demo_account(rows, selected):
    instance, calls, socket = adapter(rows, selected=selected)
    with pytest.raises(VenueError, match="^DEMO_ACCOUNT_REQUIRED$"):
        await instance.verify_demo()
    assert calls == [("GET", ACCOUNT_PATH)]
    socket.assert_not_called()


async def test_explicit_demo_selection_ignores_distinct_account_ids():
    instance, calls, socket = adapter([account(), account(OTHER)], selected=ACCOUNT)
    url, metadata = await instance._demo_target()
    assert url == OTP_URL and metadata == {"account_id": ACCOUNT, "currency": "USD"}
    assert calls == [("GET", ACCOUNT_PATH), ("POST", f"{ACCOUNT_PATH}/{ACCOUNT}/otp")]
    socket.assert_not_called()


@pytest.mark.parametrize("otp", [
    OTP_URL + "\n", OTP_URL + "\r", OTP_URL + "\t", OTP_URL + " ",
    OTP_URL.replace("fixture", "fix\tture"), OTP_URL + "\x00",
    OTP_URL + "é", OTP_URL + "#fragment", OTP_URL.replace("otp=", "other="),
    OTP_URL.replace("fixture-only", ""), OTP_URL + "&other=1",
    OTP_URL.replace("fixture-only", "%0A"), None,
])
async def test_malformed_otp_destination_never_opens_socket(otp):
    instance, calls, socket = adapter([account()], otp=otp)
    with pytest.raises(VenueError, match="^DEMO_DESTINATION_BLOCKED$"):
        await instance.verify_demo()
    assert calls == [("GET", ACCOUNT_PATH), ("POST", f"{ACCOUNT_PATH}/{ACCOUNT}/otp")]
    socket.assert_not_called()


@pytest.mark.parametrize("app_id,token,error", [
    (None, "fixture-token", "NOT_CONFIGURED"), ("fixture-app", None, "NOT_CONFIGURED"),
    ("invalid app", "fixture-token", "AUTH_ERROR"), ("fixture-app", "invalid\nvalue", "AUTH_ERROR"),
])
async def test_invalid_credentials_never_reach_transport(app_id, token, error):
    transport = Mock(side_effect=AssertionError("HTTP transport must not run"))
    socket = Mock(side_effect=AssertionError("socket must not open"))
    instance = Deriv(app_id, token, _transport=httpx.MockTransport(transport), _connect=socket)
    with pytest.raises(VenueError, match=f"^{error}$"):
        await instance.verify_demo()
    transport.assert_not_called()
    socket.assert_not_called()

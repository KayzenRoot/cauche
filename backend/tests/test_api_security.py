"""Exercise authentication, secure storage and authorization at the HTTP boundary."""

import importlib
from datetime import timedelta
from decimal import Decimal

import pytest
from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from app import main, security
from app.config import APP_ORIGIN, COOKIE_NAME
from app.db import Base, utcnow
from app.events import event
from app.models import (
    AuditEvent, BalanceSnapshot, Bot, ExchangeConnection, RealAccountConnection, RiskConfig,
    STRATEGIES, StrategyConfig, StrategyRun, SystemEvent, User,
)
from app.models import Session as StoredSession

TEST_EMAIL = "owner@example.test"
TEST_PASSWORD = "test-only demonstration password"


@pytest.fixture
def clients(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    local = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", local)
    monkeypatch.setattr(security, "SessionLocal", local)
    # Exercise Argon2id format and verification without repeating production work factors per test.
    hasher = PasswordHasher(time_cost=1, memory_cost=4096, parallelism=1)
    monkeypatch.setattr(security, "HASHER", hasher)
    monkeypatch.setattr(security, "DUMMY_HASH", hasher.hash(TEST_PASSWORD))
    bootstrap = importlib.import_module("app.bootstrap")
    monkeypatch.setattr(bootstrap, "SessionLocal", local)
    with local.begin() as db:
        db.add_all([Bot(), RiskConfig()])
        db.add_all(ExchangeConnection(exchange=venue) for venue in ("DERIV", "KALSHI"))
        db.add_all(RealAccountConnection(exchange=venue) for venue in ("DERIV", "KALSHI"))
        db.add_all(StrategyConfig(id=key) for key in STRATEGIES)
    assert bootstrap.bootstrap(TEST_EMAIL, TEST_PASSWORD)
    assert not bootstrap.bootstrap(TEST_EMAIL, TEST_PASSWORD)
    with TestClient(main.app, base_url=APP_ORIGIN) as client:
        yield client, local
    engine.dispose()


def login(client, *, email=TEST_EMAIL, password=TEST_PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password},
                       headers={"Origin": APP_ORIGIN})


def authenticate(client):
    response = login(client)
    assert response.status_code == 200
    return {"Origin": APP_ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_login_password_rotation_and_session_logout(clients):
    client, local = clients
    assert client.get("/api/auth/session").status_code == 401
    response = login(client, email=" OWNER@example.test ")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    raw = client.cookies.get(COOKIE_NAME)
    assert raw
    with local() as db:
        assert db.get(User, 1).password_hash.startswith("$argon2id$")
        assert db.get(StoredSession, raw) is None
        assert db.get(StoredSession, security.digest(raw)) is not None
    csrf = response.json()["csrf_token"]
    assert client.get("/api/auth/session").json()["csrf_token"] == csrf
    assert client.post("/api/auth/logout", headers={"Origin": APP_ORIGIN}).status_code == 403
    assert client.post("/api/auth/logout", headers={"Origin": "https://elsewhere.test",
                       "X-CSRF-Token": csrf}).status_code == 403
    fresh = "rotated test-only demonstration password"
    updated = client.post("/api/auth/password", headers={"Origin": APP_ORIGIN, "X-CSRF-Token": csrf},
                          json={"current_password": TEST_PASSWORD, "new_password": fresh,
                                "confirm": True})
    assert updated.status_code == 200
    with local() as db:
        assert db.get(StoredSession, security.digest(raw)) is None
    assert client.get("/api/auth/session").status_code == 401
    assert login(client, password=fresh).status_code == 200
    csrf = client.get("/api/auth/session").json()["csrf_token"]
    assert client.post("/api/auth/logout", headers={"Origin": APP_ORIGIN,
                       "X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/auth/session").status_code == 401


def test_throttle_and_password_input_redaction(clients):
    client, local = clients
    for _ in range(8):
        assert login(client, password="dummy-incorrect").status_code == 401
    throttled = login(client)
    assert throttled.status_code == 429 and throttled.headers["retry-after"] == "300"
    bad = "sensitive-test-placeholder-" * 10
    response = login(client, password=bad)
    assert response.status_code == 422
    assert bad not in response.text
    with local() as db:
        rows = db.scalars(select(AuditEvent).where(AuditEvent.code == "LOGIN_FAILED")).all()
        assert len(rows) == 8


def test_unauthorized_and_non_admin_denied(clients):
    client, local = clients
    assert client.get("/api/admin/overview").status_code == 401
    headers = authenticate(client)
    with local.begin() as db:
        db.get(User, 1).role = "viewer"
    assert client.get("/api/auth/session").json()["role"] == "viewer"
    assert client.get("/api/admin/overview").status_code == 403
    assert client.put("/api/admin/risk", headers=headers,
                      json={"confirm": True, "kill_switch": True}).status_code == 403


def test_credentials_encrypted_and_redacted(clients, monkeypatch):
    client, local = clients
    headers = authenticate(client)
    monkeypatch.setenv("CAUCHE_MASTER_KEY", Fernet.generate_key().decode())
    token = "fake-credential-for-isolated-test"
    response = client.put("/api/admin/connections/DERIV", headers=headers, json={
        "confirm": True,
        "credentials": {"app_id": "123", "token": token},
    })
    assert response.status_code == 200 and response.json()["configured"]
    assert token not in response.text and "ciphertext" not in response.text
    assert client.put("/api/admin/connections/DERIV", headers=headers, json={
        "confirm": True, "credentials": {"app_id": "123", "token": token, "url": "https://bad.test"},
    }).status_code == 422
    with local.begin() as db:
        row = db.get(ExchangeConnection, "DERIV")
        assert row.ciphertext and token not in row.ciphertext
        assert security.decrypt_credentials(row.ciphertext)["token"] == token
        event(db, "ERROR", "EXAMPLE", "Authorization: Bearer " + token,
              context={"token": token, "nested": {"private_key": token}})
    body = client.get("/api/events").text
    assert token not in body and "[REDACTED]" in body
    deleted = client.delete("/api/admin/connections/DERIV?confirm=true", headers=headers)
    assert deleted.status_code == 200 and not deleted.json()["configured"]


def test_event_reconnect_origin_cursor_and_scope(clients):
    client, local = clients
    authenticate(client)
    with local.begin() as db:
        event(db, "MARKET", "FIRST", "Primeiro.", exchange="DERIV", mode="PAPER")
        event(db, "MARKET", "SECOND", "Segundo.", exchange="DERIV", mode="REPLAY")
    page = client.get("/api/events?type=MARKET&limit=1").json()
    assert page["has_more"] and page["items"][0]["code"] == "SECOND"
    before = client.get(f'/api/events?type=MARKET&before={page["before"]}').json()
    assert before["items"][0]["code"] == "FIRST"
    assert client.get("/api/events?type=MARKET&mode=PAPER").json()["items"][0]["code"] == "FIRST"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws", headers={"Origin": "https://outside.test"}):
            pass
    ws_headers = {"Origin": APP_ORIGIN, "Cookie": f"{COOKIE_NAME}={client.cookies.get(COOKIE_NAME)}"}
    with client.websocket_connect(f'/api/ws?after={before["next_cursor"]}',
                                  headers=ws_headers) as ws:
        assert ws.receive_json()["code"] == "SECOND"
    with local.begin() as db:
        db.get(StoredSession, security.digest(client.cookies.get(COOKIE_NAME))).expires_at = (
            utcnow() - timedelta(seconds=1)
        )
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws", headers=ws_headers):
            pass


def test_demo_guard_kill_switch_and_bot_request_replay(clients):
    client, local = clients
    headers = authenticate(client)
    launch = {"mode": "REPLAY", "exchange": "DERIV", "strategy": "DERIV-STRAT-01",
              "instrument": "EUR/USD"}
    assert client.post("/api/bot/start", headers=headers, json={**launch, "mode": "LIVE"}).status_code == 422
    no_credentials = client.post("/api/bot/start", headers=headers, json={
        **launch, "mode": "BROKER_DEMO", "confirm": True,
    })
    assert no_credentials.json()["detail"] == "BROKER_NOT_CONFIGURED"
    activated = client.put("/api/admin/risk", headers=headers,
                           json={"confirm": True, "kill_switch": True})
    assert activated.status_code == 200
    rejected = client.post("/api/bot/start", headers=headers, json=launch)
    assert rejected.status_code == 409 and rejected.json()["detail"] == "KILL_SWITCH_ACTIVE"
    assert client.put("/api/admin/risk", headers=headers,
                      json={"confirm": True, "kill_switch": False}).status_code == 200
    started = client.post("/api/bot/start", headers=headers, json=launch)
    assert started.status_code == 202 and started.json()["state"] == "STARTING"
    assert client.post("/api/bot/start", headers=headers, json=launch).status_code == 409
    with local.begin() as db:
        assert len(db.scalars(select(StrategyRun)).all()) == 1
        db.get(Bot, 1).state = "RUNNING"
    assert client.post("/api/bot/pause", headers=headers).json()["state"] == "PAUSING"
    assert client.post("/api/bot/stop", headers=headers).json()["state"] == "STOPPING"


def test_backtest_fixture_and_portfolio_mode_isolation(clients):
    client, local = clients
    headers = authenticate(client)
    strategies = client.get("/api/strategies").json()["items"]
    assert len(strategies) == 6 and all("parameters" in item for item in strategies)
    market = client.get("/api/market?mode=REPLAY&exchange=DERIV&instrument=EUR/USD").json()
    assert market["source"] == "SIMULATED_FIXTURE" and market["candles"]
    response = client.post("/api/backtests", headers=headers, json={
        "exchange": "KALSHI", "strategy": "KALSHI-STRAT-01",
        "instrument": "FIXTURE-TEMP-NYC", "initial_balance": "1000.00",
    })
    assert response.status_code == 201 and response.json()["source"] == "SIMULATED_FIXTURE"
    assert response.json()["result"]["metrics"]["trade_count"] > 0
    assert len(client.get("/api/backtests").json()["items"]) == 1
    with local.begin() as db:
        db.add(BalanceSnapshot(
            mode="PAPER", exchange="DERIV", strategy="DERIV-STRAT-01", instrument="FIXTURE:EUR/USD",
            run_id="sim-test", source="SIMULATED_FIXTURE", initial_balance=100, balance=101,
            equity=101, realized_pnl=1, fees=0,
        ))
    assert client.get("/api/dashboard?mode=REPLAY&exchange=DERIV").json()["metrics"]["balance"] is None
    assert client.get("/api/dashboard?mode=PAPER&exchange=DERIV").json()["metrics"]["balance"] == "101.00000000"
    with local() as db:
        assert db.scalar(select(SystemEvent.id).where(SystemEvent.code == "BACKTEST_COMPLETED"))


def test_official_discovery_is_explicit_and_scoped(clients, monkeypatch):
    from app import integrations

    client, _local = clients
    authenticate(client)
    calls = []

    class MockKalshi:
        def __init__(self, *, public):
            calls.append(public)

        async def markets(self, *, category, limit):
            assert category == "Weather" and limit == 100
            return [{"instrument": "KX-EXAMPLE", "name": "Official test response",
                     "source": "KALSHI_PUBLIC" if calls[-1] else "KALSHI_DEMO",
                     "status": "active"}]

    monkeypatch.setattr(integrations, "Kalshi", MockKalshi)
    route = "/api/market/instruments"
    standby = client.get(route, params={"mode": "PAPER", "exchange": "KALSHI"})
    assert standby.json()["items"][0]["source"] == "SIMULATED_FIXTURE"
    assert calls == []
    for mode in ("PAPER", "BROKER_DEMO"):
        response = client.get(route, params={
            "mode": mode, "exchange": "KALSHI", "discover": True, "category": "Weather",
        })
        assert response.status_code == 200
        assert response.json()["items"][0]["id"] == "KX-EXAMPLE"
        assert response.json()["items"][0]["is_open"]
    assert calls == [True, False]


def test_demo_verification_needs_demo_provenance(clients, monkeypatch):
    from app import integrations

    client, _local = clients
    headers = authenticate(client)
    monkeypatch.setenv("CAUCHE_MASTER_KEY", Fernet.generate_key().decode())
    current = {"environment": "LIVE"}

    class MockDeriv:
        def __init__(self, **credentials):
            assert set(credentials) == {"app_id", "token"}

        async def verify_demo(self):
            return {**current, "currency": "USD", "source": "DERIV_DEMO",
                    "balance": Decimal("123.45"), "capabilities": ["BALANCE_READ"]}

    monkeypatch.setattr(integrations, "Deriv", MockDeriv)
    target = "/api/admin/connections/DERIV"
    saved = client.put(target, headers=headers, json={
        "confirm": True, "credentials": {"app_id": "123", "token": "fixture-credential"},
    })
    assert saved.status_code == 200 and saved.json()["status"] == "UNVERIFIED"
    denied = client.post(f"{target}/test", headers=headers, json={"confirm": True})
    assert denied.status_code == 200 and denied.json()["status"] == "AUTH_ERROR"
    assert not denied.json()["capabilities"]
    current["environment"] = "BROKER_DEMO"
    verified = client.post(f"{target}/test", headers=headers, json={"confirm": True})
    assert verified.status_code == 200 and verified.json()["status"] == "CONNECTED"
    assert verified.json()["capabilities"]["demo_balance"] == "123.45"
    assert "fixture-credential" not in verified.text


# CD-28: residual security paths, isolated sessions and ephemeral keys.
@pytest.mark.parametrize("configured", [False, True])
def test_missing_or_invalid_master_key_fails_closed(monkeypatch, configured):
    if configured:
        monkeypatch.setenv("CAUCHE_MASTER_KEY", "invalid-fixture-key")
    else:
        monkeypatch.delenv("CAUCHE_MASTER_KEY", raising=False)
    with pytest.raises(ValueError, match="^MASTER_KEY_NOT_CONFIGURED$"):
        security.encrypt_credentials({"token": "fixture"})


@pytest.mark.parametrize("payload", [b"[]", b'{"token": 1}', b"not-json", b"\xff"])
def test_decryption_rejects_invalid_authenticated_payload(monkeypatch, payload):
    key = Fernet.generate_key()
    monkeypatch.setenv("CAUCHE_MASTER_KEY", key.decode())
    ciphertext = Fernet(key).encrypt(payload).decode()
    with pytest.raises(ValueError, match="^CREDENTIALS_UNAVAILABLE$"):
        security.decrypt_credentials(ciphertext)


@pytest.mark.parametrize("wrong_key", [False, True])
def test_invalid_ciphertext_never_returns_credentials(monkeypatch, wrong_key):
    key = Fernet.generate_key()
    monkeypatch.setenv("CAUCHE_MASTER_KEY", key.decode())
    if wrong_key:
        ciphertext = Fernet(Fernet.generate_key()).encrypt(b'{"token":"fixture"}').decode()
    else:
        ciphertext = "invalid-fixture-ciphertext"
    with pytest.raises(ValueError, match="^CREDENTIALS_UNAVAILABLE$"):
        security.decrypt_credentials(ciphertext)


def test_unknown_session_token_never_authenticates(clients):
    client, local = clients
    with local() as db:
        assert security.authenticate(db, "unissued-fixture-session") is None
    client.cookies.set(COOKIE_NAME, "unissued-fixture-session")
    assert client.get("/api/auth/session").status_code == 401


def test_login_throttle_expired_window_resets(clients):
    from app.models import LoginThrottle

    client, local = clients
    assert login(client, password="incorrect-fixture").status_code == 401
    with local.begin() as db:
        row = db.scalars(select(LoginThrottle)).one()
        row.attempts = 8
        row.window_start = utcnow() - timedelta(minutes=6)
    assert login(client, password="incorrect-fixture").status_code == 401
    with local() as db:
        row = db.scalars(select(LoginThrottle)).one()
        assert row.attempts == 1
    assert login(client).status_code == 200
    with local() as db:
        assert db.scalars(select(LoginThrottle)).one().attempts == 0


def test_successful_login_upgrades_stale_password_hash(clients, monkeypatch):
    client, local = clients
    with local() as db:
        previous = db.get(User, 1).password_hash
    stronger = PasswordHasher(time_cost=2, memory_cost=4096, parallelism=1)
    assert stronger.check_needs_rehash(previous)
    monkeypatch.setattr(security, "HASHER", stronger)
    assert login(client).status_code == 200
    with local() as db:
        updated = db.get(User, 1).password_hash
        assert updated != previous
        assert stronger.verify(updated, TEST_PASSWORD)
        assert not stronger.check_needs_rehash(updated)
    assert client.get("/api/auth/session").status_code == 200


def test_real_account_settings_are_private_and_separate_from_execution(clients, monkeypatch):
    from app.integrations.real_accounts import DerivRealReader

    client, local = clients
    path = "/api/admin/real-connections/DERIV"
    assert client.get("/api/admin/real-connections").status_code == 401
    headers = authenticate(client)
    monkeypatch.setenv("CAUCHE_MASTER_KEY", Fernet.generate_key().decode())
    initial = client.get("/api/admin/real-connections").json()["items"]
    assert len(initial) == 2 and all(item["balance"] is None for item in initial)

    payload = {"confirm": True, "credentials": {"app_id": "test-app", "token": "example-token"}}
    assert client.put(path, headers=headers, json=payload).status_code == 422
    payload["credentials"]["account_id"] = "DOT90004580"
    assert client.put(path, headers={"Origin": APP_ORIGIN}, json=payload).status_code == 403
    stored = client.put(path, headers=headers, json=payload)
    assert stored.status_code == 200 and stored.json()["status"] == "UNVERIFIED"
    assert "example-token" not in stored.text and "DOT90004580" not in stored.text
    with local() as db:
        real = db.get(RealAccountConnection, "DERIV")
        assert real.ciphertext and "example-token" not in real.ciphertext
        assert db.get(ExchangeConnection, "DERIV").ciphertext is None

    async def verify(reader):
        assert reader._account_id == "DOT90004580"
        return "USD", Decimal("105.25")

    monkeypatch.setattr(DerivRealReader, "verify", verify)
    checked = client.post(path + "/test", headers=headers, json={"confirm": True})
    assert checked.status_code == 200
    result = checked.json()
    assert result["environment"] == "REAL_READ_ONLY" and result["status"] == "CONNECTED"
    assert result["capabilities"]["currency"] == "USD" and Decimal(result["balance"]) == Decimal("105.25")
    assert result["last_sync"]

    denied = client.post("/api/bot/start", headers=headers, json={
        "mode": "REAL_READ_ONLY", "exchange": "DERIV", "strategy": "DERIV-STRAT-01", "instrument": "frxEURUSD",
    })
    assert denied.status_code == 422

    cleared = client.delete(path + "?confirm=true", headers=headers)
    assert cleared.json()["status"] == "NOT_CONFIGURED"
    assert cleared.json()["balance"] is None



def test_intelligence_routes_require_owner_and_csrf(clients, monkeypatch):
    client, _database = clients
    calls = []

    async def inspect(payload):
        calls.append(payload.ticker)
        return {"ticker": payload.ticker, "decision_status": "REVIEW_REQUIRED", "extraction": None}

    monkeypatch.setattr(main, "inspect_kalshi", inspect)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    target = "/api/research/kalshi/inspect"
    for route in ("/api/research/status", "/api/research/evidence"):
        assert client.get(route).status_code == 401

    csrf = authenticate(client)
    assert client.get("/api/research/status").json() == {"model": "deepseek-flash", "configured": False}
    journal = client.get("/api/research/evidence?limit=25")
    assert journal.status_code == 200
    assert journal.json()["items"] == []
    assert client.post(target, json={"ticker": "TEST-26"}, headers={"Origin": APP_ORIGIN}).status_code == 403
    assert calls == []
    result = client.post(target, json={"ticker": "test-26"}, headers=csrf)
    assert result.status_code == 200
    assert result.json()["decision_status"] == "REVIEW_REQUIRED"
    assert calls == ["TEST-26"]

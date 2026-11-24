"""Owner-facing API and authenticated persisted-event stream."""

import asyncio
import csv
import io
import json
import logging
import re
from datetime import timedelta
from decimal import Decimal
from math import ceil
from time import monotonic
from typing import Annotated, Literal

import httpx
from fastapi import (
    Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette import status

from .commands import (
    bot_row, change_bot, checked_credentials, lock_connection_change, query_connection,
    risk_payload, risk_row, start_bot, strategy_payload,
)
from .config import APP_ORIGIN, COOKIE_NAME, LOG_RETENTION_DAYS, SECURE_COOKIE
from .db import SessionLocal, aware, lock, utcnow
from .events import audit
from .models import (
    AuditEvent, BacktestResult, BacktestRun, RealAccountConnection,
    RiskViolation, StrategyConfig, SystemEvent, User,
)
from .models import Session as StoredSession
from .queries import (
    Filters, connection, connections, dashboard, event_page, filters, history, instruments, market,
    research_evidence, scope_for_bot, scoped,
)
from .research import MODEL, EvidenceRequest, ResearchError, inspect_kalshi, model_key
from .schemas import (
    BacktestInput, ChangePassword, Confirm, ConnectionUpdate, Exchange, Launch, Login,
    Mode, RiskUpdate, StrategyId, StrategyUpdate, boundary,
)
from .security import (
    AUTH_LOCK, Identity, authenticate, check_csrf, check_origin, clear_cookie, db_session,
    decrypt_credentials, digest, encrypt_credentials, hash_password, identity, login, require_admin,
    session_payload, verify_password,
)
from .serialization import jsonable, record

VERSION = "0.1.0"
STARTED = monotonic()
logger = logging.getLogger("cauche.api")
app = FastAPI(title="CAUCHE PULSE", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)
Db = Annotated[Session, Depends(db_session)]
Owner = Annotated[Identity, Depends(identity)]
Scope = Annotated[Filters, Depends(filters)]
Limit = Annotated[int, Query(ge=1, le=500)]
Cursor = Annotated[int | None, Query(ge=0)]
ExportFormat = Literal["csv", "json"]
Severity = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
EventType = Literal["MARKET", "STRATEGY", "SIGNAL", "ORDER", "FILL", "SETTLEMENT", "RISK",
                    "API", "AUTH", "ADMIN", "SYSTEM", "ERROR"]


def write_owner(request: Request, owner: Owner) -> Identity:
    check_csrf(request, owner)
    return owner


def admin(owner: Owner) -> Identity:
    require_admin(owner)
    return owner


def admin_write(request: Request, owner: Annotated[Identity, Depends(admin)]) -> Identity:
    check_csrf(request, owner)
    return owner


Write = Annotated[Identity, Depends(write_owner)]
Admin = Annotated[Identity, Depends(admin)]
AdminWrite = Annotated[Identity, Depends(admin_write)]


@app.middleware("http")
async def security_headers(request: Request, next_handler):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        length = request.headers.get("content-length")
        if length is not None and (len(length) > 5 or not length.isascii() or
                                   not length.isdigit() or int(length) > 65536):
            return JSONResponse({"detail": "REQUEST_TOO_LARGE"}, status_code=413)
    response = await next_handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if SECURE_COOKIE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.exception_handler(RequestValidationError)
async def bad_input(_request: Request, _error: RequestValidationError):
    # FastAPI's default validation detail can contain the original secret input.
    return JSONResponse({"detail": "INVALID_INPUT"}, status_code=422)


@app.exception_handler(SQLAlchemyError)
async def database_error(_request: Request, _error: SQLAlchemyError):
    logger.error("Database operation failed")
    return JSONResponse({"detail": "DATABASE_UNAVAILABLE"}, status_code=503)


@app.get("/api/health")
def health(db: Db):
    try:
        db.execute(text("SELECT 1"))
        bot = bot_row(db)
    except (SQLAlchemyError, HTTPException):
        return JSONResponse({"status": "UNAVAILABLE"}, status_code=503)
    return {"status": "OK", "database": "OK", "worker": worker_status(bot)}


def worker_status(bot):
    at = bot.heartbeat_at
    return "ONLINE" if at and utcnow() - aware(at) < timedelta(seconds=20) else "OFFLINE"


@app.post("/api/auth/login")
def login_route(payload: Login, request: Request, response: Response, db: Db):
    check_origin(request.headers.get("origin"))
    peer = request.client.host if request.client else "unknown"
    return login(db, payload.email, payload.password, peer, response)


@app.get("/api/auth/session")
def session_route(owner: Owner):
    return session_payload(owner)


@app.post("/api/auth/logout")
def logout(db: Db, owner: Write, response: Response):
    db.execute(delete(StoredSession).where(StoredSession.token_hash == digest(owner.token)))
    audit(db, "AUTH", "LOGOUT", "Sessão encerrada.", user_id=owner.id)
    db.commit()
    clear_cookie(response)
    return {"status": "LOGGED_OUT"}


@app.post("/api/auth/password")
def change_password(payload: ChangePassword, owner: Write, db: Db, response: Response):
    lock(db, AUTH_LOCK)
    if not authenticate(db, owner.token):
        raise HTTPException(401, "UNAUTHENTICATED")
    user = db.get(User, owner.id)
    if not user or not verify_password(user.password_hash, payload.current_password):
        audit(db, "AUTH", "PASSWORD_CHANGE_DENIED", "Troca de senha recusada.",
              user_id=owner.id, severity="WARNING")
        db.commit()
        raise HTTPException(403, "INVALID_CREDENTIALS")
    if payload.new_password == payload.current_password:
        raise HTTPException(422, "PASSWORD_UNCHANGED")
    user.password_hash = hash_password(payload.new_password)
    db.execute(delete(StoredSession).where(StoredSession.user_id == owner.id))
    audit(db, "AUTH", "PASSWORD_CHANGED", "Senha alterada. Sessões anteriores encerradas.",
          user_id=owner.id)
    db.commit()
    clear_cookie(response)
    return {"status": "LOGGED_OUT"}


@app.get("/api/research/evidence")
def research_journal(
    db: Db, _owner: Admin, scope: Scope,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    return research_evidence(db, scope, limit, offset)


@app.get("/api/research/status")
def research_status(_owner: Admin):
    return {"model": MODEL, "configured": bool(model_key())}


@app.post("/api/research/kalshi/inspect")
async def research_kalshi(payload: EvidenceRequest, _owner: AdminWrite):
    try:
        return await inspect_kalshi(payload)
    except ResearchError as error:
        raise HTTPException(error.status, str(error)) from None


@app.get("/api/bot")
def bot_status(db: Db, _owner: Owner):
    return record(bot_row(db))


@app.post("/api/bot/start", status_code=status.HTTP_202_ACCEPTED)
def bot_start(payload: Launch, db: Db, owner: AdminWrite):
    return start_bot(db, payload, owner.id)


@app.post("/api/bot/{action}", status_code=status.HTTP_202_ACCEPTED)
def bot_transition(action: Literal["pause", "resume", "stop"], db: Db, owner: AdminWrite):
    return change_bot(db, action, owner.id)


@app.get("/api/dashboard")
def dashboard_route(db: Db, _owner: Owner, scope: Scope):
    return dashboard(db, scope, bot_row(db))


@app.get("/api/market")
def market_route(db: Db, _owner: Owner, scope: Scope, timeframe: int = 300):
    if timeframe not in {60, 300, 900, 1800, 3600, 14400, 86400}:
        raise HTTPException(422, "INVALID_TIMEFRAME")
    return market(db, scope, bot_row(db), timeframe)


def cached_instruments(mode: str, exchange: str) -> list[dict]:
    with SessionLocal() as db:
        return instruments(db, mode, exchange)


@app.get("/api/market/instruments")
async def market_instruments(
    _owner: Owner, mode: Mode = "REPLAY", exchange: Exchange = "DERIV", discover: bool = False,
    category: Annotated[str | None, Query(max_length=100, pattern=r"^[A-Za-z0-9 &_-]+$")] = None,
):
    if not discover or mode == "REPLAY":
        return {"items": await asyncio.to_thread(cached_instruments, mode, exchange)}

    from .integrations import Deriv, Kalshi, VenueError

    try:
        async with asyncio.timeout(12):
            if exchange == "DERIV":
                rows = await Deriv().markets()
                if category:
                    rows = [row for row in rows if row.get("category") == category]
            else:
                rows = await Kalshi(public=mode == "PAPER").markets(category=category, limit=100)
    except (VenueError, TimeoutError, OSError, httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "MARKET_DISCOVERY_UNAVAILABLE") from exc
    return {"items": [
        {"id": row["instrument"], "label": str(row["name"])[:140], "source": row["source"],
         "category": row.get("category", category),
         "is_open": row.get("is_open", row.get("status") in {"active", "open"})}
        for row in rows[:100]
    ]}


@app.get("/api/history")
def history_route(db: Db, _owner: Owner, scope: Scope, limit: Limit = 100,
                  offset: Annotated[int, Query(ge=0, le=100000)] = 0):
    return history(db, scope_for_bot(scope, bot_row(db)), limit, offset)


@app.get("/api/events")
def events_route(db: Db, _owner: Owner, scope: Scope, after: Cursor = None,
                 before: Cursor = None, limit: Limit = 100, severity: Severity | None = None,
                 q: Annotated[str | None, Query(max_length=100)] = None, type: EventType | None = None):
    return event_page(db, SystemEvent, scope, after=after, before=before, limit=limit,
                      severity=severity, q=q, type=type)


def csv_safe(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        if not re.fullmatch(r"-[0-9]+(?:\.[0-9]+)?", value):
            return "'" + value
    return value


def export(rows: list[dict], filename: str, format: ExportFormat, *, truncated: bool):
    headers = {"Content-Disposition": f'attachment; filename="{filename}.{format}"',
               "X-Export-Truncated": "true" if truncated else "false"}
    if format == "json":
        return JSONResponse({"items": rows, "truncated": truncated}, headers=headers)
    columns = list(dict.fromkeys(key for row in rows for key in row)) or ["id"]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    writer.writerows({key: csv_safe(value) for key, value in row.items()} for row in rows)
    return Response(stream.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)


@app.get("/api/events/export")
def export_events(db: Db, _owner: Owner, scope: Scope, format: ExportFormat = "csv",
                  severity: Severity | None = None,
                  q: Annotated[str | None, Query(max_length=100)] = None, type: EventType | None = None):
    page = event_page(db, SystemEvent, scope, limit=5000, severity=severity, q=q, type=type)
    return export(page["items"], "cauche-events", format, truncated=page["has_more"])


@app.get("/api/history/export")
def export_history(db: Db, _owner: Owner, scope: Scope, format: ExportFormat = "csv"):
    payload = history(db, scope_for_bot(scope, bot_row(db)), 5001)
    groups = {kind: rows for kind, rows in payload.items() if isinstance(rows, list)}
    rows = [{"record_type": kind, **row} for kind, group in groups.items() for row in group[:5000]]
    return export(rows, "cauche-history", format,
                  truncated=any(len(group) > 5000 for group in groups.values()))


def ws_page(token, after: int) -> list[dict] | None:
    with SessionLocal() as db:
        if authenticate(db, token) is None:
            return None
        return event_page(db, SystemEvent, Filters(), after=after, limit=250)["items"]


@app.websocket("/api/ws")
async def events_ws(websocket: WebSocket, after: int = 0):
    if after < 0 or websocket.headers.get("origin") != APP_ORIGIN:
        await websocket.close(code=1008)
        return
    token = websocket.cookies.get(COOKIE_NAME)
    accepted = False
    try:
        while True:
            items = await asyncio.to_thread(ws_page, token, after)
            if items is None:
                await websocket.close(code=1008)
                return
            if not accepted:
                await websocket.accept()
                accepted = True
            for item in items:
                await websocket.send_json(item)
                after = item["id"]
            if len(items) == 250:
                continue
            try:
                packet = await asyncio.wait_for(websocket.receive(), timeout=0.75)
                if packet["type"] == "websocket.disconnect":
                    return
                # This stream is receive-only for clients.
                await websocket.close(code=1008)
                return
            except TimeoutError:
                continue
    except WebSocketDisconnect:
        return
    except SQLAlchemyError:
        await websocket.close(code=1011)


@app.get("/api/strategies")
def get_strategies(db: Db, _owner: Owner):
    from .domain import catalog

    rows = {row.id: row for row in db.scalars(select(StrategyConfig))}
    return {"items": [strategy_payload(rows[spec["id"]], spec) for spec in catalog()
                      if spec["id"] in rows]}


@app.put("/api/admin/strategies/{strategy}")
def update_strategy(strategy: StrategyId, payload: StrategyUpdate, db: Db, owner: AdminWrite):
    from .domain import catalog, validate_parameters

    bot = bot_row(db, locked=True)
    row = db.get(StrategyConfig, strategy)
    if row is None:
        raise HTTPException(404, "STRATEGY_NOT_FOUND")
    try:
        parameters = jsonable(validate_parameters(strategy, payload.parameters))
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise HTTPException(422, "INVALID_STRATEGY_PARAMETERS") from exc
    if row.parameters != parameters or row.enabled != payload.enabled:
        row.parameters, row.enabled = parameters, payload.enabled
        row.version += 1
        if not row.enabled and bot.strategy == row.id and bot.state not in {"STOPPED", "STOPPING"}:
            bot.state, bot.desired_state, bot.updated_at = "STOPPING", "STOPPED", utcnow()
        audit(db, "ADMIN", "STRATEGY_UPDATED", "Configuração de estratégia atualizada.",
              user_id=owner.id, context={"strategy": row.id, "version": row.version,
                                         "enabled": row.enabled})
        db.commit()
    definition = next(item for item in catalog() if item["id"] == row.id)
    return strategy_payload(row, definition)


@app.get("/api/admin/risk")
def get_risk(db: Db, _owner: Admin):
    return risk_payload(risk_row(db))


@app.put("/api/admin/risk")
def update_risk(payload: RiskUpdate, db: Db, owner: AdminWrite):
    from .domain import validate_limits

    bot_row(db, locked=True)
    if "kill_switch" in payload.thresholds:
        raise HTTPException(422, "INVALID_RISK_THRESHOLDS")
    try:
        limits = jsonable(validate_limits({**payload.thresholds, "kill_switch": payload.kill_switch}))
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise HTTPException(422, "INVALID_RISK_THRESHOLDS") from exc
    row = risk_row(db)
    row.kill_switch = payload.kill_switch
    row.thresholds = {key: value for key, value in limits.items() if key != "kill_switch"}
    audit(db, "ADMIN", "RISK_UPDATED", "Limites de risco atualizados.", user_id=owner.id,
          context={"kill_switch": row.kill_switch, "thresholds": row.thresholds})
    db.commit()
    return risk_payload(row)


@app.get("/api/admin/risk/violations")
def risk_violations(db: Db, _owner: Admin, scope: Scope, limit: Limit = 100):
    return {"items": [record(row) for row in db.scalars(
        scoped(select(RiskViolation), RiskViolation, scope)
        .order_by(RiskViolation.id.desc()).limit(limit)
    )]}


@app.get("/api/admin/overview")
def admin_overview(db: Db, _owner: Admin):
    bot = bot_row(db)
    return {
        "status": "OK", "version": VERSION, "database": "OK", "worker": worker_status(bot),
        "uptime_seconds": int(monotonic() - STARTED), "log_retention_days": LOG_RETENTION_DAYS,
        "bot": record(bot),
        "connections": connections(db), "events": event_page(db, SystemEvent, Filters(), limit=20)["items"],
    }


@app.get("/api/admin/audit")
def security_audit(db: Db, _owner: Admin, scope: Scope, after: Cursor = None,
                   before: Cursor = None, limit: Limit = 100, severity: Severity | None = None,
                   q: Annotated[str | None, Query(max_length=100)] = None):
    return event_page(db, AuditEvent, scope, after=after, before=before, limit=limit,
                      severity=severity, q=q)


@app.get("/api/admin/sessions")
def sessions(db: Db, owner: Admin):
    current = digest(owner.token)
    rows = db.scalars(select(StoredSession)
                      .where(StoredSession.user_id == owner.id, StoredSession.expires_at > utcnow())
                      .order_by(StoredSession.created_at.desc()).limit(100))
    return {"items": [jsonable({
        "id": row.token_hash[:16], "current": row.token_hash == current,
        "created_at": row.created_at, "expires_at": row.expires_at,
    }) for row in rows]}


@app.delete("/api/admin/sessions/{session_id}")
def revoke_session(session_id: str, db: Db, owner: AdminWrite, confirm: bool = False):
    if not confirm:
        raise HTTPException(422, "CONFIRMATION_REQUIRED")
    if not re.fullmatch(r"[a-f0-9]{16}", session_id):
        raise HTTPException(404, "SESSION_NOT_FOUND")
    if digest(owner.token).startswith(session_id):
        raise HTTPException(409, "USE_LOGOUT_FOR_CURRENT_SESSION")
    lock(db, AUTH_LOCK)
    match = db.scalar(select(StoredSession).where(
        StoredSession.user_id == owner.id, StoredSession.token_hash.startswith(session_id),
    ))
    if match is None:
        raise HTTPException(404, "SESSION_NOT_FOUND")
    db.delete(match)
    audit(db, "AUTH", "SESSION_REVOKED", "Sessão remota encerrada.", user_id=owner.id)
    db.commit()
    return {"status": "REVOKED"}


@app.get("/api/admin/connections")
def list_connections(db: Db, _owner: Admin):
    return {"items": connections(db)}


@app.put("/api/admin/connections/{exchange}")
def update_connection(exchange: Exchange, payload: ConnectionUpdate, db: Db, owner: AdminWrite):
    credentials = checked_credentials(exchange, payload.credentials)
    try:
        ciphertext = encrypt_credentials(credentials)
    except ValueError as exc:
        raise HTTPException(409, "MASTER_KEY_NOT_CONFIGURED") from exc
    row = lock_connection_change(db, exchange)
    row.ciphertext, row.enabled, row.last_sync = ciphertext, payload.enabled, None
    row.status, row.capabilities = ("UNVERIFIED" if payload.enabled else "DISABLED"), {}
    audit(db, "ADMIN", "CONNECTION_UPDATED", "Credenciais demo atualizadas.", user_id=owner.id,
          context={"exchange": exchange, "enabled": payload.enabled})
    db.commit()
    return connection(row)


@app.delete("/api/admin/connections/{exchange}")
def remove_connection(exchange: Exchange, db: Db, owner: AdminWrite, confirm: bool = False):
    if not confirm:
        raise HTTPException(422, "CONFIRMATION_REQUIRED")
    row = lock_connection_change(db, exchange)
    row.ciphertext, row.enabled, row.last_sync = None, False, None
    row.status, row.capabilities = "NOT_CONFIGURED", {}
    audit(db, "ADMIN", "CONNECTION_REMOVED", "Conexão demo removida.", user_id=owner.id,
          context={"exchange": exchange})
    db.commit()
    return connection(row)


def connection_config(exchange):
    with SessionLocal() as db:
        row = query_connection(db, exchange)
        if not row.enabled or not row.ciphertext:
            raise HTTPException(409, "BROKER_NOT_CONFIGURED")
        try:
            return row.ciphertext, decrypt_credentials(row.ciphertext)
        except ValueError as exc:
            raise HTTPException(409, "CREDENTIALS_UNAVAILABLE") from exc


def apply_connection_test(exchange, ciphertext, broker_status, result, owner_id):
    with SessionLocal.begin() as db:
        bot_row(db, locked=True)
        row = query_connection(db, exchange)
        if row.ciphertext != ciphertext or not row.enabled:
            raise HTTPException(409, "CONNECTION_CHANGED")
        row.status = broker_status
        if broker_status == "CONNECTED":
            row.last_sync = utcnow()
            details = result.get("capabilities")
            balance = result.get("balance")
            # Expose only small capability identifiers supplied by the adapter.
            caps = list(details) if isinstance(details, (dict, list, tuple)) else []
            row.capabilities = {
                "resources": [value for value in caps if isinstance(value, str) and
                              re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", value)][:32],
                "currency": result.get("currency") if result.get("currency") in {"USD", "EUR", "GBP"} else None,
                "demo_balance": jsonable(balance) if isinstance(balance, Decimal) and balance.is_finite() else None,
                "source": result.get("source") if result.get("source") in {"DERIV_DEMO", "KALSHI_DEMO"} else None,
            }
        else:
            row.capabilities = {}
        audit(db, "ADMIN", "CONNECTION_TESTED", "Verificação de conexão demo registrada.",
              user_id=owner_id, context={"exchange": exchange, "status": broker_status})
        return connection(row)


@app.post("/api/admin/connections/{exchange}/test")
async def test_connection(exchange: Exchange, _payload: Confirm, owner: AdminWrite):
    from .integrations import Deriv, Kalshi, VenueError

    ciphertext, credentials = await asyncio.to_thread(connection_config, exchange)
    constructor = Deriv if exchange == "DERIV" else Kalshi
    broker_status, result = "CONNECTED", {}
    try:
        async with asyncio.timeout(15):
            result = await constructor(**credentials).verify_demo()
            if not isinstance(result, dict) or result.get("environment") != "BROKER_DEMO":
                broker_status, result = "AUTH_ERROR", {}
    except VenueError as exc:
        broker_status = "DISCONNECTED" if exc.retryable else "AUTH_ERROR"
    except (ValueError, TypeError):
        broker_status = "AUTH_ERROR"
    except (TimeoutError, OSError, httpx.HTTPError):
        broker_status = "DISCONNECTED"
    return await asyncio.to_thread(
        apply_connection_test, exchange, ciphertext, broker_status, result, owner.id,
    )


def real_account(db: Session, exchange: str, *, locked: bool = False) -> RealAccountConnection:
    query = select(RealAccountConnection).where(RealAccountConnection.exchange == exchange)
    if locked:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        raise HTTPException(503, "MIGRATE_DATABASE")
    return row


def real_connection_payload(row: RealAccountConnection) -> dict:
    return {
        "exchange": row.exchange,
        "environment": "REAL_READ_ONLY",
        "configured": bool(row.ciphertext),
        "enabled": row.enabled,
        "status": row.status,
        "last_sync": jsonable(row.last_sync),
        "balance": jsonable(row.balance),
        "capabilities": {"currency": row.currency} if row.currency else {},
    }


def set_real_connection(
    db: Session, exchange: str, owner_id: int, ciphertext: str | None, *, enabled: bool,
) -> dict:
    row = real_account(db, exchange, locked=True)
    row.ciphertext = ciphertext
    row.enabled = enabled
    row.status = ("UNVERIFIED" if enabled else "DISABLED") if ciphertext else "NOT_CONFIGURED"
    row.last_sync, row.balance, row.currency = None, None, None
    audit(db, "ADMIN", "REAL_CONNECTION_UPDATED", "Configuração de consulta real alterada.",
          user_id=owner_id, context={"exchange": exchange, "status": row.status})
    db.commit()
    return real_connection_payload(row)


@app.get("/api/admin/real-connections")
def list_real_connections(db: Db, _owner: Admin):
    return {"items": [real_connection_payload(row) for row in db.scalars(
        select(RealAccountConnection).order_by(RealAccountConnection.exchange),
    )]}


@app.put("/api/admin/real-connections/{exchange}")
def update_real_connection(exchange: Exchange, payload: ConnectionUpdate, db: Db, owner: AdminWrite):
    credentials = checked_credentials(exchange, payload.credentials)
    if exchange == "DERIV" and not credentials.get("account_id"):
        raise HTTPException(422, "INVALID_CREDENTIAL_FIELDS")
    try:
        ciphertext = encrypt_credentials(credentials)
    except ValueError as exc:
        raise HTTPException(409, "MASTER_KEY_NOT_CONFIGURED") from exc
    return set_real_connection(db, exchange, owner.id, ciphertext, enabled=payload.enabled)


@app.delete("/api/admin/real-connections/{exchange}")
def remove_real_connection(exchange: Exchange, db: Db, owner: AdminWrite, confirm: bool = False):
    if not confirm:
        raise HTTPException(422, "CONFIRMATION_REQUIRED")
    return set_real_connection(db, exchange, owner.id, None, enabled=False)


def real_account_config(exchange: str) -> tuple[str, dict]:
    with SessionLocal() as db:
        row = real_account(db, exchange)
        if not row.enabled or not row.ciphertext:
            raise HTTPException(409, "BROKER_NOT_CONFIGURED")
        try:
            return row.ciphertext, decrypt_credentials(row.ciphertext)
        except ValueError as exc:
            raise HTTPException(409, "CREDENTIALS_UNAVAILABLE") from exc


def apply_real_account_check(
    exchange: str, ciphertext: str, broker_status: str,
    observed: tuple[str, Decimal] | None, owner_id: int,
) -> dict:
    with SessionLocal.begin() as db:
        row = real_account(db, exchange, locked=True)
        if not row.enabled or row.ciphertext != ciphertext:
            raise HTTPException(409, "CONNECTION_CHANGED")
        row.status = broker_status
        row.currency, row.balance = observed if observed else (None, None)
        row.last_sync = utcnow() if observed else None
        audit(db, "ADMIN", "REAL_CONNECTION_TESTED", "Consulta à conta real registrada.",
              user_id=owner_id, context={"exchange": exchange, "status": broker_status})
        return real_connection_payload(row)


@app.post("/api/admin/real-connections/{exchange}/test")
async def test_real_connection(exchange: Exchange, _payload: Confirm, owner: AdminWrite):
    from .integrations import VenueError
    from .integrations.real_accounts import DerivRealReader, KalshiRealReader

    ciphertext, credentials = await asyncio.to_thread(real_account_config, exchange)
    constructor = DerivRealReader if exchange == "DERIV" else KalshiRealReader
    try:
        async with asyncio.timeout(15):
            observed = await constructor(**credentials).verify()
        broker_status = "CONNECTED"
    except VenueError as exc:
        observed = None
        broker_status = "DISCONNECTED" if exc.retryable else "AUTH_ERROR"
    except (TimeoutError, OSError, httpx.HTTPError):
        observed, broker_status = None, "DISCONNECTED"
    except (ValueError, TypeError):
        observed, broker_status = None, "AUTH_ERROR"
    return await asyncio.to_thread(
        apply_real_account_check, exchange, ciphertext, broker_status, observed, owner.id,
    )


def backtest_payload(run: BacktestRun, result: BacktestResult) -> dict:
    return {**record(run), "result": jsonable(result.result)}


@app.get("/api/backtests")
def backtest_history(db: Db, _owner: Owner, limit: Limit = 30):
    runs = db.execute(select(BacktestRun, BacktestResult)
                      .join(BacktestResult, BacktestResult.run_id == BacktestRun.id)
                      .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc()).limit(limit))
    return {"items": [backtest_payload(run, result) for run, result in runs]}


@app.post("/api/backtests", status_code=status.HTTP_201_CREATED)
def execute_backtest(payload: BacktestInput, db: Db, owner: Write):
    from .domain import backtest

    if payload.strategy.split("-")[0] != payload.exchange:
        raise HTTPException(422, "STRATEGY_EXCHANGE_MISMATCH")
    try:
        lower, upper = boundary(payload.start), boundary(payload.end, upper=True)
        if lower is not None and upper is not None and lower >= upper:
            raise ValueError
        result = jsonable(backtest(
            payload.exchange, payload.strategy, payload.instrument.removeprefix("FIXTURE:"),
            initial_balance=payload.initial_balance, parameters=payload.parameters,
            costs=payload.costs, risk=payload.risk,
            start=ceil(lower.timestamp()) if lower else None,
            end=ceil(upper.timestamp()) - 1 if upper else None,
        ))
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        raise HTTPException(422, "INVALID_BACKTEST_INPUTS") from exc
    run = BacktestRun(exchange=payload.exchange, strategy=payload.strategy,
                      instrument=payload.instrument, inputs=payload.model_dump(mode="json"))
    db.add(run)
    db.flush()
    details = BacktestResult(run_id=run.id, result=result)
    db.add(details)
    audit(db, "ADMIN", "BACKTEST_COMPLETED", "Simulação histórica concluída.",
          user_id=owner.id, context={"run_id": run.id, "strategy": run.strategy})
    db.commit()
    return backtest_payload(run, details)

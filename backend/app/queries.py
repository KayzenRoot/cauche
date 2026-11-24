"""Read models for the market terminal, metrics and activity."""

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .db import aware, utcnow
from .models import (
    BalanceSnapshot, Bot, ExchangeConnection, Fee, Fill, MarketCandle, MarketSnapshot,
    Order, Position, Settlement, Signal,
)
from .schemas import Exchange, Instrument, Mode, StrategyId, boundary
from .serialization import jsonable, record

OPEN_ORDERS = (
    "PROPOSED", "SUBMITTING", "PENDING", "OPEN", "ACCEPTED", "RESTING", "PARTIAL",
    "PARTIALLY_FILLED", "UNKNOWN", "CANCEL_PENDING",
)
OPEN_POSITIONS = ("OPEN", "PARTIAL", "ACTIVE")
FIXTURES = {
    "DERIV": ("EUR/USD", "GBP/USD", "USD/JPY"),
    "KALSHI": ("FIXTURE-TEMP-NYC",),
}


def fixture_instrument(mode: str, instrument: str) -> bool:
    return mode == "REPLAY" or (mode == "PAPER" and instrument.startswith("FIXTURE"))


@dataclass(frozen=True)
class Filters:
    mode: str | None = None
    exchange: str | None = None
    strategy: str | None = None
    instrument: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    currency: str | None = None


def filters(
    mode: Mode | None = None,
    exchange: Exchange | None = None,
    strategy: StrategyId | None = None,
    instrument: Instrument | None = None,
    start: str | None = Query(default=None, max_length=40),
    end: str | None = Query(default=None, max_length=40),
    currency: str | None = Query(default=None, min_length=3, max_length=8, pattern=r"^[A-Z]{3,8}$"),
) -> Filters:
    try:
        lower, upper = boundary(start), boundary(end, upper=True)
        if lower and upper and lower >= upper:
            raise ValueError
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, "INVALID_TIME_RANGE") from exc
    return Filters(mode, exchange, strategy, instrument, lower, upper, currency)


def scoped(statement, model, scope: Filters, *, dates=True):
    for attr in ("mode", "exchange", "strategy", "instrument", "currency"):
        value = getattr(scope, attr)
        if value is not None and hasattr(model, attr):
            statement = statement.where(getattr(model, attr) == value)
    if dates and scope.start:
        statement = statement.where(model.timestamp >= scope.start)
    if dates and scope.end:
        statement = statement.where(model.timestamp < scope.end)
    return statement


def event_page(session: Session, model, scope: Filters, *, after=None, before=None,
               limit=100, severity=None, q=None, type=None) -> dict:
    statement = scoped(select(model), model, scope)
    if severity:
        statement = statement.where(model.severity == severity)
    if type:
        statement = statement.where(model.type == type)
    if q:
        statement = statement.where(or_(
            model.message.icontains(q, autoescape=True),
            model.code.icontains(q, autoescape=True),
            model.correlation_id.icontains(q, autoescape=True),
        ))
    if after is not None:
        statement = statement.where(model.id > after)
    if before is not None:
        statement = statement.where(model.id < before)
    ordering = model.id if after is not None and before is None else model.id.desc()
    items = list(session.scalars(statement.order_by(ordering).limit(limit + 1)))
    page = items[:limit]
    return {
        "items": [record(item) for item in page],
        "next_cursor": max((item.id for item in page), default=after),
        "before": min((item.id for item in page), default=before),
        "has_more": len(items) > limit,
    }


def connection(row: ExchangeConnection) -> dict:
    return {
        **record(row, exclude=("ciphertext",)),
        "environment": "BROKER_DEMO",
        "configured": bool(row.ciphertext),
    }


def connections(session: Session) -> list[dict]:
    return [connection(row) for row in session.scalars(
        select(ExchangeConnection).order_by(ExchangeConnection.exchange)
    )]


def scope_for_bot(scope: Filters, bot: Bot) -> Filters:
    return replace(scope, mode=scope.mode or bot.mode, exchange=scope.exchange or bot.exchange)


def history(session: Session, scope: Filters, limit: int, offset: int = 0) -> dict:
    tables = (Order, Fill, Position, Settlement, Fee)
    payload = {
        model.__tablename__: [
            record(row, exclude=("data",)) for row in session.scalars(
                scoped(select(model), model, scope)
                .order_by(model.timestamp.desc(), model.id.desc()).limit(limit).offset(offset)
            )
        ]
        for model in tables
    }
    return {**payload, "offset": offset, "limit": limit}


def _amounts(session, scope):
    results = session.execute(scoped(
        select(
            Settlement.currency,
            func.sum(Settlement.net_pnl),
            func.sum(Settlement.gross_pnl),
            func.sum(Settlement.fees),
            func.count(),
            func.count().filter(Settlement.net_pnl > 0),
        ).group_by(Settlement.currency),
        Settlement, scope,
    )).all()
    if len(results) > 1:
        return None, None, None, None, sum(row[4] for row in results), None
    if results:
        return tuple(results[0])
    return None, None, None, None, 0, None


def _current_count(session, model, scope, open_statuses):
    statement = scoped(select(func.count()).select_from(model), model, scope, dates=False)
    return session.scalar(statement.where(model.status.in_(open_statuses)))


def _paid_fees(session, scope, settlement_currency, fallback):
    totals = dict(session.execute(scoped(
        select(Fee.currency, func.sum(Fee.amount)).group_by(Fee.currency), Fee, scope,
    )).all())
    if len(totals) > 1 or (settlement_currency and totals and settlement_currency not in totals):
        return None
    return next(iter(totals.values()), fallback)


def _strategy_summary(session, scope):
    rows = session.execute(scoped(
        select(
            Settlement.strategy, Settlement.currency, func.count(), func.sum(Settlement.net_pnl),
            func.count().filter(Settlement.net_pnl > 0),
        ).group_by(Settlement.strategy, Settlement.currency),
        Settlement, scope,
    ))
    return [
        {
            "strategy": strategy, "currency": currency, "trade_count": count,
            "net_pnl": net, "win_rate": Decimal(wins) / count, "expectancy": net / count,
        }
        for strategy, currency, count, net, wins in rows
    ]


def _balance_series(session, scope, snapshot):
    if not snapshot:
        return [], None
    # The worker creates virtual balances per run. Never splice reset runs into a single equity line.
    series_scope = replace(scope, currency=snapshot.currency)
    statement = scoped(
        select(BalanceSnapshot).where(BalanceSnapshot.run_id == snapshot.run_id),
        BalanceSnapshot, series_scope,
    )
    items = list(session.scalars(
        statement.order_by(BalanceSnapshot.timestamp.desc(), BalanceSnapshot.id.desc()).limit(300)
    ))
    max_drawdown = session.scalar(scoped(
        select(func.max(BalanceSnapshot.drawdown)).where(BalanceSnapshot.run_id == snapshot.run_id),
        BalanceSnapshot, series_scope,
    ))
    return [
        {"time": int(aware(row.timestamp).timestamp()), "value": row.equity, "source": row.source}
        for row in reversed(items)
    ], max_drawdown


def _period_results(session, scope):
    trading_day = func.date(Settlement.timestamp)
    grouped = scoped(
        select(trading_day, Settlement.currency, func.sum(Settlement.net_pnl),
               func.sum(Settlement.fees), func.count())
        .group_by(trading_day, Settlement.currency)
        .order_by(trading_day.desc()).limit(730),
        Settlement, scope,
    )
    periods = {name: {} for name in ("daily", "weekly", "monthly")}
    for raw_day, currency, pnl, fees, count in session.execute(grouped):
        day = raw_day if isinstance(raw_day, date) else date.fromisoformat(raw_day)
        starts = {
            "daily": day,
            "weekly": day - timedelta(days=day.weekday()),
            "monthly": day.replace(day=1),
        }
        for name, start in starts.items():
            item = periods[name].setdefault((start, currency), {
                "start": start, "currency": currency, "net_pnl": Decimal("0"),
                "fees": Decimal("0"), "trade_count": 0,
            })
            item["net_pnl"] += pnl
            item["fees"] += fees
            item["trade_count"] += count
    return {name: list(results.values()) for name, results in periods.items()}


def dashboard(session: Session, scope: Filters, bot: Bot) -> dict:
    from .models import SystemEvent

    scope = scope_for_bot(scope, bot)
    currency, net, gross, fees, count, wins = _amounts(session, scope)
    snapshot = session.scalar(scoped(select(BalanceSnapshot), BalanceSnapshot, scope)
                              .order_by(BalanceSnapshot.id.desc()).limit(1))
    same_currency = snapshot is not None and (currency in {None, snapshot.currency})
    equity, max_drawdown = _balance_series(session, scope, snapshot)
    run_start = snapshot.initial_balance if snapshot else None
    # A period may extend over multiple independent simulated accounts.
    run_count = session.scalar(scoped(select(func.count(func.distinct(Settlement.run_id))),
                                      Settlement, scope))
    roi = None
    if snapshot and same_currency and snapshot.initial_balance > 0 and run_count == 1:
        unique_run = session.scalar(scoped(select(Settlement.run_id), Settlement, scope).distinct())
        if unique_run == snapshot.run_id and net is not None:
            roi = net / snapshot.initial_balance
    metrics = {
        "currency": currency or (snapshot.currency if snapshot else scope.currency),
        "balance_currency": snapshot.currency if snapshot else None,
        "initial_balance": run_start,
        "balance": snapshot.balance if snapshot else None,
        "equity": snapshot.equity if snapshot else None,
        "realized_pnl": net,
        "unrealized_pnl": snapshot.unrealized_pnl if snapshot else None,
        "gross_pnl": gross,
        "net_pnl": net,
        "fees": _paid_fees(session, scope, currency, fees),
        "roi": roi,
        "win_rate": Decimal(wins) / count if wins is not None and count else None,
        "trade_count": count,
        "open_orders": _current_count(session, Order, scope, OPEN_ORDERS),
        "open_positions": _current_count(session, Position, scope, OPEN_POSITIONS),
        "drawdown": snapshot.drawdown if snapshot else None,
        "max_drawdown": max_drawdown,
        "expectancy": net / count if net is not None and count else None,
        "balance_timestamp": snapshot.timestamp if snapshot else None,
        "source": snapshot.source if snapshot else None,
        "run_id": snapshot.run_id if snapshot else None,
    }
    rows = history(session, scope, 12)
    latest_by_scope = select(func.max(BalanceSnapshot.id)).group_by(
        BalanceSnapshot.mode, BalanceSnapshot.exchange, BalanceSnapshot.currency,
    )
    balances = session.scalars(
        select(BalanceSnapshot).where(BalanceSnapshot.id.in_(latest_by_scope))
        .order_by(BalanceSnapshot.mode, BalanceSnapshot.exchange, BalanceSnapshot.currency)
    )
    return jsonable({
        "mode": scope.mode, "exchange": scope.exchange, "instrument": scope.instrument,
        "metrics": metrics, "orders": rows["orders"], "positions": rows["positions"],
        "equity": equity, "events": event_page(session, SystemEvent, scope, limit=20)["items"],
        "bot": record(bot), "mode_summaries": [record(row) for row in balances],
        "strategy_summaries": _strategy_summary(session, scope),
        "period_results": _period_results(session, scope),
        "connections": connections(session),
    })


def instruments(session, mode, exchange):
    if mode == "REPLAY":
        return [{"id": value, "label": value, "source": "SIMULATED_FIXTURE"}
                for value in FIXTURES[exchange]]
    items = [
        {"id": key, "label": key, "source": source} for key, source in session.execute(
            select(MarketSnapshot.instrument, MarketSnapshot.source)
            .where(MarketSnapshot.mode == mode, MarketSnapshot.exchange == exchange,
                   ~MarketSnapshot.instrument.startswith("FIXTURE"))
            .distinct().order_by(MarketSnapshot.instrument).limit(100)
        )
    ]
    if mode == "PAPER":
        items += [
            {"id": f"FIXTURE:{value}" if exchange == "DERIV" else value,
             "label": value, "source": "SIMULATED_FIXTURE"}
            for value in FIXTURES[exchange]
        ]
    return items


def _bucket(epoch: int, timeframe: int) -> int:
    # Venue bars are recorded using their close times.
    return ((epoch - 1) // timeframe + 1) * timeframe


def _resample(candles: list[dict], timeframe: int, base_frame: int) -> list[dict]:
    groups = defaultdict(list)
    for bar in candles:
        groups[_bucket(bar["time"], timeframe)].append(bar)
    return [
        {"time": time, "open": bars[0]["open"],
         "high": max((bar["high"] for bar in bars), key=Decimal),
         "low": min((bar["low"] for bar in bars), key=Decimal),
         "close": bars[-1]["close"],
         "closed": bars[-1]["time"] >= time and len(bars) == timeframe // base_frame,
         "volume": sum(Decimal(bar["volume"]) for bar in bars)
         if all(bar["volume"] is not None for bar in bars) else None}
        for time, bars in groups.items()
    ]


def _preview(exchange, instrument, active):
    from .fixtures import dataset

    try:
        frames = dataset(exchange, instrument.removeprefix("FIXTURE:"))
    except (ValueError, KeyError):
        return [], 300
    if not frames:
        return [], 300
    # Static fixture preview gives the owner enough history to inspect before a run.
    # An active run may only show data already reached by its own cursor.
    observations = frames[:1] if active else frames[:160]
    by_time = {bar.time: bar for observation in observations for bar in observation.candles}
    return [bar.as_dict() for bar in by_time.values()], frames[0].granularity


def market(session: Session, scope: Filters, bot: Bot, timeframe: int) -> dict:
    scope = scope_for_bot(scope, bot)
    mode, exchange = scope.mode, scope.exchange
    if mode is None or exchange is None:
        raise RuntimeError("Market scope requires mode and exchange")
    selected = scope.instrument or (
        bot.instrument if bot.exchange == exchange and bot.mode == mode else None
    )
    if selected is None and mode == "REPLAY":
        selected = FIXTURES[exchange][0]
    marker_scope = replace(scope, instrument=selected)
    scope = replace(marker_scope, strategy=None)
    active = (
        bot.state not in {"STOPPED", "ERROR"} and bool(bot.run_id) and bot.mode == mode
        and bot.exchange == exchange and bot.instrument == selected
    )
    snapshot_query = scoped(select(MarketSnapshot), MarketSnapshot, scope, dates=False)
    if active:
        snapshot_query = snapshot_query.where(MarketSnapshot.run_id == bot.run_id)
    snapshot = session.scalar(snapshot_query.order_by(MarketSnapshot.id.desc()).limit(1)) if selected else None

    candles = []
    source = snapshot.source if snapshot else None
    base_frame = snapshot.data.get("granularity", 300) if snapshot else 300
    if selected and source:
        query = select(MarketCandle).where(MarketCandle.source == source,
                                           MarketCandle.timeframe == base_frame)
        if snapshot and active:
            query = query.where(MarketCandle.time <= int(aware(snapshot.timestamp).timestamp()))
        if scope.start:
            query = query.where(MarketCandle.time >= int(scope.start.timestamp()))
        if scope.end:
            query = query.where(MarketCandle.time < int(scope.end.timestamp()))
        rows = session.scalars(scoped(query, MarketCandle, scope, dates=False)
                               .order_by(MarketCandle.time.desc()).limit(480))
        candles = [record(row, exclude=("run_id", "id", "timestamp", "mode", "exchange",
                                        "strategy", "instrument", "source", "timeframe"))
                   for row in reversed(list(rows))]
    if selected and not snapshot and fixture_instrument(mode, selected):
        candles, base_frame = _preview(exchange, selected, active)
        source = "SIMULATED_FIXTURE"
        candles = [bar for bar in candles
                   if (not scope.start or bar["time"] >= scope.start.timestamp())
                   and (not scope.end or bar["time"] < scope.end.timestamp())]
    if timeframe < base_frame:
        candles = []
    elif timeframe > base_frame:
        candles = _resample(candles, timeframe, base_frame)
    last_sync = snapshot.timestamp if snapshot else None
    status = snapshot.status if snapshot else ("SIMULATED" if candles else "DISCONNECTED")
    if not fixture_instrument(mode, selected or "") and last_sync:
        if (utcnow() - aware(last_sync)).total_seconds() > 60:
            status = "STALE"
        if bot.mode == mode and bot.exchange == exchange and bot.state == "DISCONNECTED":
            status = "DISCONNECTED"

    def marker_query(model, *conditions):
        query = scoped(select(model).where(*conditions), model, marker_scope)
        if active:
            query = query.where(model.run_id == bot.run_id)
        return query.order_by(model.timestamp.desc()).limit(100)

    signals = session.scalars(marker_query(Signal, Signal.direction != "NO_SIGNAL"))
    markers = []
    for row in signals:
        observed = row.data.get("market_time")
        markers.append({"time": observed if isinstance(observed, int) else
                        int(aware(row.timestamp).timestamp()), "kind": "SIGNAL", "label": row.direction})
    for model, label in ((Fill, "FILL"), (Settlement, "SETTLEMENT")):
        rows = session.scalars(marker_query(model))
        markers.extend({"time": int(aware(row.timestamp).timestamp()), "kind": label, "label": label}
                       for row in rows)
    if timeframe > base_frame:
        for marker in markers:
            marker["time"] = _bucket(marker["time"], timeframe)
    mark = snapshot.data.get("mark") if snapshot else None
    if snapshot and mark is not None:
        stream_timestamp = snapshot.data.get("stream_timestamp")
        if not isinstance(stream_timestamp, int) or not 0 <= utcnow().timestamp() - stream_timestamp <= 20:
            mark = None
    if mark is None and candles:
        mark = candles[-1]["close"]
    return jsonable({
        "source": source, "status": status, "exchange": exchange, "instrument": selected,
        "timeframe": timeframe, "native_timeframe": base_frame,
        "last_sync": last_sync, "mark": mark,
        "book": snapshot.data.get("book") if snapshot else None,
        "contracts": snapshot.data.get("contracts", []) if snapshot else [],
        "candles": candles, "markers": markers,
    })

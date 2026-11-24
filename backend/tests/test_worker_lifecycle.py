"""CD-12: shutdown, failure handling and secret-safe logging, without broker I/O."""

import asyncio
import logging
import runpy
import signal
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import OperationalError

from app import worker


@pytest.fixture
def lifecycle(monkeypatch):
    loop = asyncio.get_running_loop
    handlers = {}
    ticker = Mock()
    ticker.select = AsyncMock()
    monkeypatch.setattr(worker, "MarketTicker", Mock(return_value=ticker))
    monkeypatch.setattr(worker, "MARKET_TICK", 0.001)

    def register(signum, callback):
        handlers[signum] = callback

    async def install():
        monkeypatch.setattr(loop(), "add_signal_handler", register)

    return ticker, handlers, install


async def test_loop_graceful_shutdown_after_retryable_failures(lifecycle, monkeypatch, caplog):
    ticker, handlers, install = lifecycle
    await install()
    calls = []

    async def tick(value):
        assert value is ticker
        calls.append("tick")
        if len(calls) == 1:
            raise OperationalError("fixture", {}, RuntimeError("PRIVATE_DATABASE_DETAIL"))
        if len(calls) == 2:
            raise RuntimeError("PRIVATE_UPSTREAM_DETAIL")
        handlers[signal.SIGTERM]()
        return 0.1

    monkeypatch.setattr(worker, "leased_step", tick)
    with caplog.at_level(logging.INFO, logger="cauche.worker"):
        await asyncio.wait_for(worker.loop(), timeout=5)
    assert calls == ["tick", "tick", "tick"]
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
    ticker.select.assert_awaited_once_with(None)
    assert "worker_start" in caplog.text and "worker_stop" in caplog.text
    assert "worker_database_unavailable" in caplog.text
    assert "worker_unexpected_error" in caplog.text
    assert "PRIVATE_DATABASE_DETAIL" not in caplog.text
    assert "PRIVATE_UPSTREAM_DETAIL" not in caplog.text


async def test_loop_signal_registration_fallback(lifecycle, monkeypatch):
    ticker, handlers, install = lifecycle
    await install()

    def unsupported(*_):
        raise NotImplementedError

    def fallback(signum, handler):
        handlers[signum] = handler

    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", unsupported)
    monkeypatch.setattr(worker.signal, "signal", fallback)

    async def tick(_):
        handlers[signal.SIGINT](signal.SIGINT, None)
        return 0.1

    monkeypatch.setattr(worker, "leased_step", tick)
    await asyncio.wait_for(worker.loop(), timeout=5)
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
    ticker.select.assert_awaited_once_with(None)


async def test_loop_cancelled_mid_tick_always_stops_ticker(lifecycle, monkeypatch):
    ticker, _, install = lifecycle
    await install()
    entered = asyncio.Event()

    async def tick(_):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "leased_step", tick)
    task = asyncio.create_task(worker.loop())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    ticker.select.assert_awaited_once_with(None)


def test_main_calls_async_worker(monkeypatch):
    execute = AsyncMock()
    configure = Mock()
    monkeypatch.setattr(worker, "loop", execute)
    monkeypatch.setattr(worker.logging, "basicConfig", configure)
    worker.main()
    execute.assert_awaited_once_with()
    configure.assert_called_once()


def test_module_entrypoint_delegates_to_worker_loop(monkeypatch):
    # Execute the actual python -m entrypoint while intercepting only the
    # infinite event-loop boundary: no daemon, database or remote I/O.
    def run(coroutine):
        try:
            assert coroutine.cr_code.co_name == "loop"
            assert coroutine.cr_frame.f_globals["__name__"] == "__main__"
        finally:
            coroutine.close()

    execute = Mock(side_effect=run)
    configure = Mock()
    monkeypatch.setattr(asyncio, "run", execute)
    monkeypatch.setattr(worker.logging, "basicConfig", configure)
    monkeypatch.delitem(sys.modules, "app.worker")
    runpy.run_module("app.worker", run_name="__main__", alter_sys=True)
    execute.assert_called_once()
    configure.assert_called_once()

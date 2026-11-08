"""Pytest configuration and Windows sandbox asyncio compatibility hooks."""

import asyncio
import asyncio.proactor_events
import inspect
import signal
import sys
from typing import Any


class _DummySelfSocket:
    """Lightweight placeholder when loopback TCP socketpair is blocked by OS sandbox."""

    def fileno(self) -> int:
        return -1

    def close(self) -> None:
        pass

    def send(self, _data: bytes) -> int:
        return 0


def _ensure_windows_asyncio_sandbox_compat() -> None:
    """Configure Windows ProactorEventLoop to run without loopback TCP socketpair."""
    if sys.platform != "win32":
        return

    def _sandbox_make_self_pipe(self: asyncio.proactor_events.BaseProactorEventLoop) -> None:
        self._ssock = _DummySelfSocket()
        self._csock = _DummySelfSocket()
        self._internal_fds += 1

    def _sandbox_close_self_pipe(self: asyncio.proactor_events.BaseProactorEventLoop) -> None:
        if getattr(self, "_self_reading_future", None) is not None:
            self._self_reading_future.cancel()
            self._self_reading_future = None
        self._ssock = None
        self._csock = None
        self._internal_fds -= 1

    def _sandbox_loop_self_reading(
        self: asyncio.proactor_events.BaseProactorEventLoop, f: object = None
    ) -> None:
        return None

    def _sandbox_write_to_self(self: asyncio.proactor_events.BaseProactorEventLoop) -> None:
        return None

    asyncio.proactor_events.BaseProactorEventLoop._make_self_pipe = _sandbox_make_self_pipe  # type: ignore[method-assign]
    asyncio.proactor_events.BaseProactorEventLoop._close_self_pipe = _sandbox_close_self_pipe  # type: ignore[method-assign]
    asyncio.proactor_events.BaseProactorEventLoop._loop_self_reading = _sandbox_loop_self_reading  # type: ignore[method-assign]
    asyncio.proactor_events.BaseProactorEventLoop._write_to_self = _sandbox_write_to_self  # type: ignore[method-assign]
    signal.set_wakeup_fd = lambda _fd, **_kw: -1  # type: ignore[assignment]


_ensure_windows_asyncio_sandbox_compat()


def pytest_pyfunc_call(pyfuncitem: Any) -> bool | None:
    """Execute async test coroutines deterministically via asyncio.run."""
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        funcargs = pyfuncitem.funcargs
        testargs = {arg: funcargs[arg] for arg in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(pyfuncitem.obj(**testargs))
        return True
    return None

"""Shared Postgres session must not nest transactions across threads."""

from __future__ import annotations

import threading
from types import TracebackType
from typing import Any, Self

from obsalt.store.postgres import SerialConnection


class _FakeTx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> Self:
        with self._conn.guard:
            self._conn.depth += 1
            self._conn.max_depth = max(self._conn.max_depth, self._conn.depth)
            if self._conn.depth > 1:
                raise RuntimeError("nested transaction")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        with self._conn.guard:
            self._conn.depth -= 1


class _FakeConn:
    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.depth = 0
        self.max_depth = 0

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def transaction(self) -> _FakeTx:
        return _FakeTx(self)

    def rollback(self) -> None:
        return None


def test_serial_connection_prevents_cross_thread_nested_transactions() -> None:
    wrapped = SerialConnection(_FakeConn())
    errors: list[BaseException] = []

    def _work() -> None:
        try:
            for _ in range(20):
                with wrapped.transaction():
                    wrapped.execute("SELECT 1")
        except BaseException as exc:  # noqa: BLE001 — collect for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=_work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert wrapped._conn.max_depth == 1  # type: ignore[attr-defined]


def test_serial_connection_same_thread_reenter_execute_inside_transaction() -> None:
    wrapped = SerialConnection(_FakeConn())
    with wrapped.transaction():
        wrapped.execute("SELECT 1")
    assert wrapped._conn.max_depth == 1  # type: ignore[attr-defined]

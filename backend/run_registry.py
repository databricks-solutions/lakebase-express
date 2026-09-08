"""In-memory run state, written through to the configured RunStore.

Replaces the per-module ``{run_id: state}`` dicts. Memory stays the hot path for
polling; reads fall back to the store, so a run owned by another worker (or
started before a restart) is still visible.

Progress ticks fire per COPY batch, so writes are throttled: a status change or a
terminal state flushes at once, anything else at most every ``_FLUSH_SECONDS``.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Generic, TypeVar

from pydantic import BaseModel

from backend.run_store import RunRecord, RunStore, get_run_store

log = logging.getLogger("lakebase_express.run_registry")

S = TypeVar("S", bound=BaseModel)

_TERMINAL = frozenset({"success", "failed", "partial"})
_FLUSH_SECONDS = 2.0


class RunRegistry(Generic[S]):
    """Registry for one kind of run (``data_migration``, ``validation``, …)."""

    def __init__(self, kind: str, model: type[S], store: RunStore | None = None):
        self._kind = kind
        self._model = model
        self._store = store
        self._runs: dict[str, S] = {}
        self._flushed: dict[str, float] = {}
        self._version: dict[str, int] = {}
        self._written: dict[str, int] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    @property
    def store(self) -> RunStore:
        if self._store is None:
            self._store = get_run_store()
        return self._store

    def create(self, state: S) -> None:
        run_id = state.run_id  # type: ignore[attr-defined]
        with self._lock:
            self._runs[run_id] = state
            self._flushed[run_id] = time.monotonic()
            self._version[run_id] = version = self._version.get(run_id, 0) + 1
        self._persist(state.model_copy(deep=True), version)

    def get(self, run_id: str) -> S | None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is not None:
                # Copy so the caller never observes a half-mutated object.
                return state.model_copy(deep=True)
        return self._load(run_id)

    def update(self, run_id: str, mutate: Callable[[S], object]) -> None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return
            before = getattr(state, "status", None)
            mutate(state)
            after = getattr(state, "status", None)
            now = time.monotonic()
            due = (
                after != before
                or after in _TERMINAL
                or now - self._flushed.get(run_id, 0.0) >= _FLUSH_SECONDS
            )
            if not due:
                return
            self._flushed[run_id] = now
            self._version[run_id] = version = self._version.get(run_id, 0) + 1
            snapshot = state.model_copy(deep=True)
        # Outside the lock: a store write must not block pollers.
        self._persist(snapshot, version)

    def list(self, limit: int = 50) -> list[RunRecord]:
        try:
            return self.store.list(self._kind, limit)
        except Exception as exc:
            log.warning("Could not list %s runs: %s", self._kind, exc)
            return []

    def _persist(self, state: S, version: int) -> None:
        run_id = state.run_id  # type: ignore[attr-defined]
        with self._write_lock:
            # Snapshots are taken under the main lock but written outside it, so an
            # older one can arrive late. Dropping it keeps a finished run from being
            # overwritten by a stale "running".
            if self._written.get(run_id, 0) >= version:
                return
            self._written[run_id] = version
            self._save(state)

    def _save(self, state: S) -> None:
        try:
            self.store.save(
                self._kind,
                state.run_id,  # type: ignore[attr-defined]
                getattr(state, "status", "") or "",
                state.model_dump(mode="json"),
            )
        except Exception as exc:
            # Persistence is observability, not the migration — never fail the run.
            log.warning("Could not persist %s run %s: %s", self._kind, state.run_id, exc)  # type: ignore[attr-defined]

    def _load(self, run_id: str) -> S | None:
        try:
            data = self.store.load(self._kind, run_id)
        except Exception as exc:
            log.warning("Could not load %s run %s: %s", self._kind, run_id, exc)
            return None
        if data is None:
            return None
        try:
            return self._model.model_validate(data)
        except Exception as exc:
            log.warning("Stored %s run %s is unreadable: %s", self._kind, run_id, exc)
            return None

"""Applies plan items (schema + code) to Lakebase.

Each item runs in its own transaction so a single failure is isolated — the
object is reported as failed and the rest continue (unless stop_on_error). Items
are applied in dependency order (schema → tables → functions → views →
procedures → triggers).

Transient failures retry the item on a fresh connection; real errors (bad SQL,
missing dependency, constraint violation) are reported on the first attempt.
"""
from __future__ import annotations

import logging
import time

from backend.connectors.lakebase import LakebaseConnection, transient_reason
from backend.migration.models import (
    KIND_ORDER,
    ItemResult,
    ItemStatus,
    ObjectKind,
    PlanItem,
)
from backend.retry import DB_POLICY, call_with_retry
from backend.schema_migration.trigger_sql import sanitize_trigger_sql

log = logging.getLogger("lakebase_express.executor")


def _ordered(items: list[PlanItem]) -> list[PlanItem]:
    return [it for _, it in sorted(enumerate(items), key=lambda p: (KIND_ORDER[p[1].kind], p[0]))]


def _item_sql(item: PlanItem) -> str:
    """SQL to apply for a plan item. Trigger DDL is sanitized here — at apply
    time — so a plan built before the fix (or hand-edited) still applies cleanly:
    the model schema-qualifies trigger names (a Postgres syntax error) and omits
    OR REPLACE. Other kinds pass through untouched."""
    if item.kind is ObjectKind.TRIGGER:
        return sanitize_trigger_sql(item.sql)
    return item.sql


class _Session:
    """Reopenable Lakebase connection. Teardown is best-effort — the connection we
    are replacing is usually already broken, and must not mask the real error."""

    def __init__(self, conn_info: LakebaseConnection) -> None:
        self._info = conn_info
        self.conn = self._open()

    def _open(self):
        conn = self._info.connect()
        conn.autocommit = False
        return conn

    def rollback(self) -> None:
        try:
            self.conn.rollback()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def reopen(self) -> None:
        self.close()
        self.conn = self._open()


def apply_plan(
    conn_info: LakebaseConnection,
    items: list[PlanItem],
    stop_on_error: bool = False,
) -> list[ItemResult]:
    results: list[ItemResult] = []
    session = _Session(conn_info)
    try:
        for item in _ordered(items):
            if not item.sql.strip():
                results.append(
                    ItemResult(id=item.id, name=item.name, kind=item.kind, status=ItemStatus.SKIPPED,
                               error="No SQL to apply.")
                )
                continue

            t0 = time.perf_counter()

            def apply_item(item=item) -> None:
                with session.conn.cursor() as cur:
                    cur.execute(_item_sql(item))
                session.conn.commit()

            def rollback_and_reconnect() -> None:
                # Whatever broke the statement usually left the socket unusable.
                session.rollback()
                session.reopen()

            try:
                call_with_retry(
                    apply_item,
                    policy=DB_POLICY,
                    transient=transient_reason,
                    what=f"apply {item.name}",
                    log=log,
                    before_retry=rollback_and_reconnect,
                )
                results.append(
                    ItemResult(id=item.id, name=item.name, kind=item.kind, status=ItemStatus.SUCCESS,
                               duration_ms=int((time.perf_counter() - t0) * 1000))
                )
            except Exception as exc:
                session.rollback()
                log.warning("Apply failed for %s: %s", item.name, exc)
                results.append(
                    ItemResult(id=item.id, name=item.name, kind=item.kind, status=ItemStatus.FAILED,
                               error=str(exc), duration_ms=int((time.perf_counter() - t0) * 1000))
                )
                if stop_on_error:
                    break
    finally:
        session.close()
    return results

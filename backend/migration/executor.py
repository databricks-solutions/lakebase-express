"""Applies plan items (schema + code) to Lakebase.

Each item runs in its own transaction so a single failure is isolated — the
object is reported as failed and the rest continue (unless stop_on_error). Items
are applied in dependency order (schema → tables → functions → views →
procedures → triggers).
"""
from __future__ import annotations

import logging
import time

from backend.connectors.lakebase import LakebaseConnection
from backend.migration.models import (
    KIND_ORDER,
    ItemResult,
    ItemStatus,
    ObjectKind,
    PlanItem,
)
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


def apply_plan(
    conn_info: LakebaseConnection,
    items: list[PlanItem],
    stop_on_error: bool = False,
) -> list[ItemResult]:
    results: list[ItemResult] = []
    conn = conn_info.connect()
    conn.autocommit = False
    try:
        for item in _ordered(items):
            if not item.sql.strip():
                results.append(
                    ItemResult(id=item.id, name=item.name, kind=item.kind, status=ItemStatus.SKIPPED,
                               error="No SQL to apply.")
                )
                continue

            t0 = time.perf_counter()
            try:
                with conn.cursor() as cur:
                    cur.execute(_item_sql(item))
                conn.commit()
                results.append(
                    ItemResult(id=item.id, name=item.name, kind=item.kind, status=ItemStatus.SUCCESS,
                               duration_ms=int((time.perf_counter() - t0) * 1000))
                )
            except Exception as exc:
                conn.rollback()
                log.warning("Apply failed for %s: %s", item.name, exc)
                results.append(
                    ItemResult(id=item.id, name=item.name, kind=item.kind, status=ItemStatus.FAILED,
                               error=str(exc), duration_ms=int((time.perf_counter() - t0) * 1000))
                )
                if stop_on_error:
                    break
    finally:
        conn.close()
    return results

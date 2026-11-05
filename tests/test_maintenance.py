"""Post-migration maintenance: the sync step and the async job's last notebook."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from backend.data_migration.etl_generator import generate, run_task_keys
from backend.data_migration.models import DataGenRequest, TableRef, TuningOverrides
from backend.migration import maintenance
from backend.migration.models import (
    LakebaseConnRequest,
    MaintenanceRequest,
    MaintenanceRunState,
    MaintenanceTableRef,
)
from backend.run_registry import RunRegistry
from backend.run_store import MemoryRunStore

_DENIED = 'permission denied to vacuum "locked", skipping it'


class _Conn:
    """psycopg-ish connection: records SQL, fails or warns on chosen tables."""

    def __init__(self, log, fail=(), warn=()):
        self.log, self.fail, self.warn = log, fail, warn
        self.autocommit = False
        self.closed = self.broken = False
        self._handlers = []

    def add_notice_handler(self, fn):
        self._handlers.append(fn)

    def execute(self, sql, params=None):
        self.log.append((self.autocommit, sql))
        if any(f'"{t}"' in sql for t in self.fail):
            raise RuntimeError("relation does not exist")
        if any(f'"{t}"' in sql for t in self.warn):
            for h in self._handlers:
                h(SimpleNamespace(severity_nonlocalized="WARNING", message_primary=_DENIED))

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                conn.execute(sql, params)

        return _Cur()

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Target:
    def __init__(self, **kw):
        self.log: list = []
        self.kw = kw

    def connect(self):
        return _Conn(self.log, **self.kw)


# --- Sync step ---------------------------------------------------------------------


def test_vacuums_every_table_on_an_autocommit_connection_without_timeout():
    tgt = _Target()
    results = maintenance.maintain_tables(tgt, [("public", "orders"), ("sales", 'odd"name')])
    assert [r.status for r in results] == ["success", "success"]
    # VACUUM can't run in a transaction; a server timeout must not cut it short.
    assert all(autocommit for autocommit, _ in tgt.log)
    assert [sql for _, sql in tgt.log] == [
        "SET statement_timeout = 0",
        'VACUUM (ANALYZE) "public"."orders"',
        'VACUUM (ANALYZE) "sales"."odd""name"',
    ]


def test_one_failed_table_does_not_stop_the_rest():
    tgt = _Target(fail=("gone",))
    results = maintenance.maintain_tables(tgt, [("public", "gone"), ("public", "orders")])
    assert [(r.name, r.status) for r in results] == [
        ("public.gone", "failed"), ("public.orders", "success"),
    ]
    assert "does not exist" in results[0].error


def test_a_skipped_table_warning_is_a_failure_not_a_success():
    """VACUUM only WARNs when it lacks rights on a table; that must not read as done."""
    results = maintenance.maintain_tables(
        _Target(warn=("locked",)), [("public", "locked"), ("public", "orders")]
    )
    assert results[0].status == "failed" and results[0].error == _DENIED
    assert results[1].status == "success" and results[1].error is None


def _request(**over):
    base = dict(
        lakebase=LakebaseConnRequest(host="lb", database="db", user="u", password="p"),
        target_schema="public",
        tables=[MaintenanceTableRef(schema_name="dbo", table_name="Orders"),
                MaintenanceTableRef(schema_name="Sales", table_name="Invoice")],
    )
    base.update(over)
    return MaintenanceRequest(**base)


def test_targets_follow_the_projects_naming_rules():
    assert maintenance.target_tables(_request()) == [("public", "orders"), ("sales", "invoice")]


def test_run_records_each_table_and_settles(monkeypatch):
    registry = RunRegistry("maintenance", MaintenanceRunState, store=MemoryRunStore())
    tgt = _Target(fail=("invoice",))
    monkeypatch.setattr(maintenance, "_REGISTRY", registry)
    monkeypatch.setattr(maintenance, "LakebaseConnection", lambda **kw: tgt)
    monkeypatch.setattr(maintenance.audit, "spawn", lambda fn, *a: fn(*a))

    state = maintenance.get_run(maintenance.start_run(_request()))
    assert state.status == "partial" and state.finished_at
    assert [(t.name, t.status) for t in state.tables] == [
        ("public.orders", "success"), ("sales.invoice", "failed"),
    ]


# --- Async job's last task -----------------------------------------------------------


def _spec(**over):
    base = dict(
        host="h", database="db", username="u", password_secret_key="k",
        lakebase_host="lb-host", lakebase_user="lbuser", lakebase_password_secret_key="lb-key",
        tables=[TableRef(schema_name="dbo", table_name="Orders"),
                TableRef(schema_name="Sales", table_name="Invoice")],
    )
    base.update(over)
    return DataGenRequest(**base)


def test_maintenance_is_the_last_task_by_default():
    assert run_task_keys(_spec())[-1] == "maintenance"
    assert generate(_spec())[-1].filename == "02_maintenance.py"


def test_turning_it_off_drops_the_task():
    spec = _spec(tuning=TuningOverrides(maintenance=False))
    assert "maintenance" not in run_task_keys(spec)
    assert all("maintenance" not in a.filename for a in generate(spec))


def _run_notebook(monkeypatch, code, log, **conn_kw):
    """Exec the notebook's Python with fake dbutils/psycopg."""
    body = "\n".join(
        line for line in code.splitlines()
        if not line.startswith("# MAGIC") and line != "# Databricks notebook source"
        and line.strip() != "# COMMAND ----------"
    )
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(
        connect=lambda **kw: (log.append(("connect", kw.get("autocommit"))), _Conn(log, **conn_kw))[1],
    ))
    dbutils = SimpleNamespace(
        library=SimpleNamespace(restartPython=lambda: None),
        secrets=SimpleNamespace(get=lambda scope, key: "pw"),
    )
    exec(body, {"dbutils": dbutils})


def test_notebook_vacuums_the_mapped_target_tables(monkeypatch):
    log: list = []
    _run_notebook(monkeypatch, generate(_spec())[-1].code, log)
    assert log[0] == ("connect", True)
    assert [sql for _, sql in log[1:]] == [
        "SET statement_timeout = 0",
        'VACUUM (ANALYZE) "public"."orders"',
        'VACUUM (ANALYZE) "sales"."invoice"',
    ]


def test_notebook_carries_on_past_a_failure_then_fails_the_task(monkeypatch):
    log: list = []
    with pytest.raises(RuntimeError, match="1 table"):
        _run_notebook(monkeypatch, generate(_spec())[-1].code, log, fail=("orders",))
    assert 'VACUUM (ANALYZE) "sales"."invoice"' in [sql for _, sql in log[1:]]

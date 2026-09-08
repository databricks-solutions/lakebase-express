"""Transient-error retry across the migration engine (backend/retry.py).

Covers the Azure SQL scan, plan executor, data loader and foundation-model calls.
Each site is checked both ways: a blip is survived, a real error fails at once.
"""
import psycopg
import pymssql
import pytest
from databricks.sdk.errors import (
    BadRequest,
    DeadlineExceeded,
    InternalError,
    PermissionDenied,
    TemporarilyUnavailable,
    TooManyRequests,
    Unauthenticated,
)
from psycopg import errors as pgerr

from backend import fm_params, retry
from backend.connectors import azure_sql
from backend.connectors.azure_sql import AzureSqlConnection, transient_reason
from backend.connectors.lakebase import transient_reason as pg_transient
from backend.fm_params import query_chat
from backend.fm_params import transient_reason as fm_transient
from backend.migration import data_loader, executor
from backend.migration.models import ObjectKind, PlanItem, TableLoadSpec
from backend.retry import (
    DB_POLICY,
    FM_POLICY,
    LOAD_POLICY,
    RetryPolicy,
    call_with_retry,
)


@pytest.fixture
def sleeps(monkeypatch):
    """Record sleeps instead of waiting."""
    recorded: list[float] = []
    monkeypatch.setattr(retry.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def no_jitter(monkeypatch):
    """Pin the jittered policies to their base delays so waits are assertable."""
    monkeypatch.setattr(retry.random, "random", lambda: 0.0)


@pytest.fixture
def serving(monkeypatch):
    """Serving-endpoint double; tests replace ``.query`` with their own failures."""
    holder: dict = {}

    class _Endpoint:
        def query(self, name, messages, **params):
            return {"ok": True, "params": params}

    class _W:
        @property
        def serving_endpoints(self):
            return holder["ep"]

    monkeypatch.setattr(fm_params, "FM_API", "serving")
    monkeypatch.setattr(fm_params, "workspace_client", lambda: _W())

    def install() -> _Endpoint:
        holder["ep"] = _Endpoint()
        return holder["ep"]

    return install


def _spec() -> TableLoadSpec:
    return TableLoadSpec(schema_name="dbo", table_name="t", target_table="t", total_rows=1000)


# --- the shared policy -------------------------------------------------------


def test_backoff_repeats_its_last_wait_when_attempts_outrun_it():
    policy = RetryPolicy(attempts=5, backoff=(1.0, 2.0))
    assert [policy.delay(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 2.0, 2.0]


def test_backoff_is_exact_without_jitter():
    assert [DB_POLICY.delay(n) for n in (1, 2, 3)] == [5.0, 10.0, 20.0]


def test_jitter_only_ever_extends_a_wait(monkeypatch):
    # Bounded by [base, base * (1 + jitter)] — never shorter than the base.
    monkeypatch.setattr(retry.random, "random", lambda: 1.0)
    assert FM_POLICY.delay(1) == pytest.approx(2.0 * 1.25)
    monkeypatch.setattr(retry.random, "random", lambda: 0.0)
    assert FM_POLICY.delay(1) == 2.0


def test_non_transient_failure_raises_the_original_exception(sleeps):
    sentinel = ValueError("real problem")

    def boom():
        raise sentinel

    with pytest.raises(ValueError) as caught:
        call_with_retry(boom, policy=DB_POLICY, transient=lambda e: None,
                        what="thing", log=retry.logging.getLogger("t"))
    assert caught.value is sentinel  # not wrapped
    assert sleeps == []


def test_before_retry_runs_between_attempts_but_not_after_the_last(sleeps):
    calls, resets = [], []

    def boom():
        calls.append(1)
        raise ValueError("transient")

    with pytest.raises(ValueError):
        call_with_retry(boom, policy=RetryPolicy(attempts=3, backoff=(1.0,)),
                        transient=lambda e: "always", what="thing",
                        log=retry.logging.getLogger("t"),
                        before_retry=lambda: resets.append(1))
    assert len(calls) == 3
    assert len(resets) == 2  # once per wait, never after the final failure
    assert sleeps == [1.0, 1.0]


# --- Azure SQL source (pymssql) ----------------------------------------------


class _FakeCursor:
    def execute(self, sql):
        pass

    def fetchall(self):
        return [{"ok": 1}]


class _FakeConn:
    def cursor(self, as_dict=False):
        return _FakeCursor()

    def close(self):
        pass


def _conn() -> AzureSqlConnection:
    return AzureSqlConnection(
        host="srv.database.windows.net", database="db", username="u@srv", password="pw"
    )


def _resuming_error() -> pymssql.OperationalError:
    return pymssql.OperationalError(
        (40613, b"Database 'db' on server 'srv' is not currently available. "
                b"Please retry the connection later.")
    )


def test_retries_resume_error_then_succeeds(monkeypatch, sleeps):
    attempts = []

    def connect(**kwargs):
        attempts.append(kwargs)
        if len(attempts) < 3:
            raise _resuming_error()
        return _FakeConn()

    monkeypatch.setattr(azure_sql.pymssql, "connect", connect)
    assert _conn().test_connection() is True
    assert len(attempts) == 3
    assert sleeps == [5.0, 10.0]


def test_non_transient_error_raises_immediately(monkeypatch, sleeps):
    attempts = []

    def connect(**kwargs):
        attempts.append(kwargs)
        raise pymssql.OperationalError((18456, b"Login failed for user 'u@srv'."))

    monkeypatch.setattr(azure_sql.pymssql, "connect", connect)
    with pytest.raises(pymssql.OperationalError):
        _conn().query("SELECT 1")
    assert len(attempts) == 1
    assert sleeps == []


def test_gives_up_after_max_attempts(monkeypatch, sleeps):
    attempts = []

    def connect(**kwargs):
        attempts.append(kwargs)
        raise _resuming_error()

    monkeypatch.setattr(azure_sql.pymssql, "connect", connect)
    with pytest.raises(pymssql.OperationalError):
        _conn().query("SELECT 1")
    assert len(attempts) == DB_POLICY.attempts
    assert sleeps == [5.0, 10.0, 20.0]


def test_transient_detection_by_code():
    assert transient_reason(_resuming_error()) == "error 40613"
    for code in sorted(azure_sql._TRANSIENT_CODES):
        assert transient_reason(pymssql.OperationalError((code, b"busy"))) is not None


def test_transient_detection_by_message_marker():
    # FreeTDS sometimes layers DB-Lib noise around the real message with a
    # non-transient leading code — the wording still identifies the resume.
    exc = pymssql.OperationalError(
        (20018, b"General SQL Server error: Database 'db' is not currently available.")
    )
    assert transient_reason(exc) is not None


def test_non_pymssql_errors_are_not_transient():
    assert transient_reason(ValueError("40613")) is None
    assert transient_reason(pymssql.OperationalError((4060, b"Cannot open database"))) is None


# --- Lakebase target (psycopg) -----------------------------------------------


@pytest.mark.parametrize("error", [
    pgerr.AdminShutdown, pgerr.CannotConnectNow, pgerr.TooManyConnections,
    pgerr.SerializationFailure, pgerr.DeadlockDetected, pgerr.lookup("08006"),
])
def test_postgres_transient_sqlstates(error):
    assert pg_transient(error("boom")) is not None


@pytest.mark.parametrize("error", [
    pgerr.ForeignKeyViolation,   # 23503 — the load left orphan rows
    pgerr.UniqueViolation,       # 23505
    pgerr.SyntaxError,           # 42601 — bad translated SQL
    pgerr.UndefinedTable,        # 42P01 — missing dependency
])
def test_postgres_real_errors_are_not_transient(error):
    assert pg_transient(error("boom")) is None


def test_bad_password_is_not_transient_despite_being_operational():
    # The "no SQLSTATE means the socket died" shortcut must not sweep this up.
    exc = pgerr.InvalidPassword("password authentication failed")
    assert isinstance(exc, psycopg.OperationalError)
    assert pg_transient(exc) is None


def test_connection_failure_without_a_sqlstate_is_transient():
    assert pg_transient(psycopg.OperationalError("connection failed: timeout")) is not None


def test_non_psycopg_errors_are_not_transient():
    assert pg_transient(ValueError("08006")) is None


# --- foundation-model calls --------------------------------------------------


@pytest.mark.parametrize("error", [
    TooManyRequests, InternalError, TemporarilyUnavailable, DeadlineExceeded,
])
def test_endpoint_transient_errors(error):
    assert fm_transient(error("boom")) is not None


@pytest.mark.parametrize("error", [BadRequest, PermissionDenied, Unauthenticated])
def test_endpoint_real_errors_are_not_transient(error):
    assert fm_transient(error("boom")) is None


def test_rate_limited_call_is_retried_then_succeeds(serving, sleeps, no_jitter):
    fake = serving()
    calls = []

    def rate_limited(name, messages, **params):
        calls.append(dict(params))
        if len(calls) < 3:
            raise TooManyRequests("REQUEST_LIMIT_EXCEEDED")
        return {"ok": True, "params": params}

    fake.query = rate_limited
    assert query_chat("databricks-claude-sonnet-5", [], max_tokens=10)["ok"]
    assert len(calls) == 3
    assert sleeps == [2.0, 6.0]


def test_bad_request_is_not_retried(serving, sleeps):
    fake = serving()
    calls = []

    def boom(name, messages, **params):
        calls.append(1)
        raise BadRequest("BAD_REQUEST: malformed messages")

    fake.query = boom
    with pytest.raises(BadRequest):
        query_chat("databricks-claude-sonnet-5", [], max_tokens=10)
    assert len(calls) == 1
    assert sleeps == []


def test_negotiated_parameters_survive_a_transient_retry(serving, sleeps, no_jitter):
    """Negotiation and transient retry are separate loops — if the retry restarted
    from the caller's parameters it would rediscover the rejection every attempt."""
    fake = serving()
    calls = []

    def picky(name, messages, **params):
        calls.append(dict(params))
        if "temperature" in params:
            raise Exception(f"BAD_REQUEST: Model {name} does not support the temperature parameter.")
        if len(calls) < 3:
            raise TemporarilyUnavailable("503 upstream")
        return {"ok": True, "params": params}

    fake.query = picky
    assert query_chat("databricks-future-model", [], temperature=0.2, max_tokens=10)["ok"]
    # 1: rejected on temperature. 2: temperature dropped, hits 503. 3: succeeds.
    assert [("temperature" in c) for c in calls] == [True, False, False]
    assert sleeps == [2.0]  # only the 503 waited; the rejection retried at once


# --- plan executor -----------------------------------------------------------


class _FakePgCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append(sql)
        if self._conn.fail_with:
            raise self._conn.fail_with.pop(0)

    def fetchone(self):
        return (None,)  # to_regclass finds nothing

    def fetchall(self):
        return []


class _FakePg:
    """psycopg double; ``fail_with`` raises once per execute, in order."""

    def __init__(self, fail_with=None):
        self.executed: list[str] = []
        self.fail_with = list(fail_with or [])
        self.autocommit = True
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return _FakePgCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _FakeTarget:
    """LakebaseConnection double, handing out queued connections in turn."""

    def __init__(self, *conns):
        self._queue = list(conns)
        self.opened: list[_FakePg] = []

    def connect(self):
        conn = self._queue.pop(0) if self._queue else _FakePg()
        self.opened.append(conn)
        return conn


def _item(sql: str = "CREATE TABLE t (id int)") -> PlanItem:
    return PlanItem(id="table:t", kind=ObjectKind.TABLE, name="public.t", sql=sql)


def test_executor_retries_a_transient_item_on_a_fresh_connection(sleeps):
    dead = _FakePg(fail_with=[pgerr.AdminShutdown("server closed the connection")])
    live = _FakePg()
    target = _FakeTarget(dead, live)

    results = executor.apply_plan(target, [_item()])

    assert [r.status.value for r in results] == ["success"]
    assert len(target.opened) == 2 and target.opened[1] is live
    assert dead.closed and live.commits == 1
    assert sleeps == [5.0]


def test_executor_reports_a_real_error_without_retrying(sleeps):
    pg = _FakePg(fail_with=[pgerr.SyntaxError("syntax error at or near")])
    target = _FakeTarget(pg)

    results = executor.apply_plan(target, [_item("CREATE TABL t")])

    assert [r.status.value for r in results] == ["failed"]
    assert "syntax error" in results[0].error
    assert len(pg.executed) == 1          # tried once
    assert len(target.opened) == 1        # never reconnected
    assert sleeps == []


def test_executor_gives_up_after_the_policy_and_reports_the_item_failed(sleeps):
    target = _FakeTarget(*[
        _FakePg(fail_with=[pgerr.AdminShutdown("gone")]) for _ in range(DB_POLICY.attempts)
    ])

    results = executor.apply_plan(target, [_item()])

    assert [r.status.value for r in results] == ["failed"]
    assert len(target.opened) == DB_POLICY.attempts
    assert sleeps == [5.0, 10.0, 20.0]


def test_executor_keeps_going_after_a_failed_item():
    target = _FakeTarget(_FakePg(fail_with=[pgerr.SyntaxError("bad")]))
    results = executor.apply_plan(target, [_item("BAD"), _item("CREATE TABLE u (id int)")])
    assert [r.status.value for r in results] == ["failed", "success"]


# --- data loader -------------------------------------------------------------


def test_load_table_retries_and_rewinds_reported_progress(monkeypatch, sleeps):
    # A retry re-copies from row zero, so reported progress must go back too.
    attempts = []
    progress: list[int] = []

    def once(*args, **kwargs):
        attempts.append(1)
        on_progress = args[6]
        on_progress(500)
        if len(attempts) < 2:
            raise pgerr.AdminShutdown("server closed the connection")
        return 1000

    monkeypatch.setattr(data_loader, "_load_table_once", once)

    total = data_loader.load_table(
        object(), object(), _spec(), "public", True, 100, progress.append,
    )
    assert total == 1000
    assert len(attempts) == 2
    assert progress == [500, 0, 500]  # rewound before the second attempt
    assert sleeps == [5.0]


def test_load_table_does_not_retry_a_real_error(monkeypatch, sleeps):
    attempts = []

    def once(*args, **kwargs):
        attempts.append(1)
        raise pgerr.UndefinedTable('relation "public.t" does not exist')

    monkeypatch.setattr(data_loader, "_load_table_once", once)

    with pytest.raises(pgerr.UndefinedTable):
        data_loader.load_table(object(), object(), _spec(), "public", True, 100, lambda n: None)
    assert len(attempts) == 1
    assert sleeps == []


def test_load_table_retries_a_source_side_blip(monkeypatch, sleeps):
    # The loader classifies both ends, not just Postgres.
    attempts = []

    def once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 2:
            raise _resuming_error()
        return 7

    monkeypatch.setattr(data_loader, "_load_table_once", once)
    assert data_loader.load_table(
        object(), object(), _spec(), "public", True, 100, lambda n: None
    ) == 7
    assert len(attempts) == 2


def test_load_table_gives_up_under_the_narrower_load_policy(monkeypatch, sleeps):
    # Fewer attempts than DB_POLICY: each one re-copies the whole table.
    attempts = []

    def once(*args, **kwargs):
        attempts.append(1)
        raise pgerr.AdminShutdown("gone")

    monkeypatch.setattr(data_loader, "_load_table_once", once)

    with pytest.raises(pgerr.AdminShutdown):
        data_loader.load_table(object(), object(), _spec(), "public", True, 100, lambda n: None)
    assert len(attempts) == LOAD_POLICY.attempts < DB_POLICY.attempts
    assert sleeps == [5.0, 15.0]


def test_capture_and_drop_fks_is_retried(sleeps):
    target = _FakeTarget(
        _FakePg(fail_with=[pgerr.AdminShutdown("gone")]),
        _FakePg(),
    )
    assert data_loader.capture_and_drop_fks(target, ['"public"."t"']) == []
    assert len(target.opened) == 2
    assert sleeps == [5.0]


def test_restore_fks_retries_a_blip_but_reports_orphan_rows(sleeps):
    orphans = pgerr.ForeignKeyViolation("violates foreign key constraint")
    pg = _FakePg(fail_with=[pgerr.AdminShutdown("gone"), orphans])
    target = _FakeTarget(pg)

    failures = data_loader.restore_fks(target, [("public.t", "fk_t", "FOREIGN KEY (a) REFERENCES u(b)")])

    # First execute blipped; the retry hit the real violation, which is reported.
    assert len(failures) == 1 and "fk_t" in failures[0]
    assert len(pg.executed) == 2
    assert sleeps == [5.0]

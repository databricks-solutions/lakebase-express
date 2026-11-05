"""Connection reuse: a session serves many queries on one connection per side (#16)."""
from contextlib import contextmanager

import pytest

from backend.connectors import azure_sql, lakebase
from backend.connectors.azure_sql import AzureSqlConnection
from backend.connectors.lakebase import LakebaseConnection

# --- Azure SQL ------------------------------------------------------------------


class _MssqlCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql):
        if "boom" in sql:
            raise azure_sql.pymssql.OperationalError("DBPROCESS is dead")
        self._conn.executed.append(sql)

    def fetchall(self):
        return [{"ok": 1}]


class _MssqlConn:
    def __init__(self, kw):
        self.kw = kw
        self.executed: list[str] = []
        self.closed = False

    def cursor(self, as_dict=False):
        return _MssqlCursor(self)

    def close(self):
        self.closed = True


@pytest.fixture
def mssql(monkeypatch):
    opened: list[_MssqlConn] = []

    def connect(**kw):
        opened.append(_MssqlConn(kw))
        return opened[-1]

    monkeypatch.setattr(azure_sql.pymssql, "connect", connect)
    return opened


def _source() -> AzureSqlConnection:
    return AzureSqlConnection(host="srv", database="db", username="u", password="pw")


def test_source_session_reuses_one_autocommit_connection(mssql):
    with _source().session() as s:
        assert s.database == "db"
        for _ in range(5):
            assert s.query("SELECT 1 AS ok") == [{"ok": 1}]
    assert len(mssql) == 1
    assert mssql[0].kw["autocommit"] is True
    assert mssql[0].executed == ["SELECT 1 AS ok"] * 5
    assert mssql[0].closed


def test_source_session_keeps_one_connection_per_timeout(mssql):
    with _source().session() as s:
        s.query("SELECT 1")
        s.query("SELECT COUNT_BIG(*)", timeout=600)
        s.query("SELECT 2")
        s.query("SELECT COUNT_BIG(*)", timeout=600)
    assert [c.kw["timeout"] for c in mssql] == [120, 600]
    assert all(c.closed for c in mssql)


def test_source_session_reconnects_after_a_failed_query(mssql):
    with _source().session() as s:
        s.query("SELECT 1")
        with pytest.raises(azure_sql.pymssql.OperationalError):
            s.query("SELECT boom")
        assert mssql[0].closed
        s.query("SELECT 2")
    assert len(mssql) == 2
    assert mssql[1].executed == ["SELECT 2"]


def test_source_one_shot_query_still_opens_its_own_connection(mssql):
    conn = _source()
    conn.query("SELECT 1")
    conn.query("SELECT 2")
    assert len(mssql) == 2
    assert mssql[0].kw["autocommit"] is False


# --- Lakebase -------------------------------------------------------------------


class _PgCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.log.append(sql)

    def fetchall(self):
        return [{"n": 1}]


class _PgConn:
    def __init__(self):
        self.log: list[str] = []
        self.autocommit = False
        self.closed = False
        self.broken = False

    def cursor(self, row_factory=None):
        return _PgCursor(self)

    @contextmanager
    def transaction(self):
        self.log.append("BEGIN")
        yield
        self.log.append("COMMIT")

    def close(self):
        self.closed = True


@pytest.fixture
def pg(monkeypatch):
    opened: list[_PgConn] = []

    def connect(self):
        opened.append(_PgConn())
        return opened[-1]

    monkeypatch.setattr(LakebaseConnection, "connect", connect)
    return opened


def _target() -> LakebaseConnection:
    return LakebaseConnection(host="h", database="tgt", user="u", password="pw")


def test_target_session_reuses_one_autocommit_connection(pg):
    with _target().session() as s:
        assert s.database == "tgt"
        s.query("SELECT 1")
        s.execute("ANALYZE t")
        s.query("SELECT 2")
    assert len(pg) == 1
    assert pg[0].autocommit is True
    assert pg[0].log == ["SELECT 1", "ANALYZE t", "SELECT 2"]
    assert pg[0].closed


def test_target_session_scopes_statement_timeout_to_its_query(pg):
    with _target().session() as s:
        s.query("SELECT count(*)", statement_timeout_ms=600_000)
        s.query("SELECT 1")
    assert pg[0].log == [
        "BEGIN", "SET LOCAL statement_timeout = 600000", "SELECT count(*)", "COMMIT",
        "SELECT 1",
    ]


def test_target_session_reopens_a_broken_connection(pg):
    with _target().session() as s:
        s.query("SELECT 1")
        pg[0].broken = True
        s.query("SELECT 2")
    assert len(pg) == 2
    assert pg[0].closed and pg[1].log == ["SELECT 2"]


def test_target_session_mints_the_oauth_credential_once_per_connection(monkeypatch):
    minted: list[str] = []
    monkeypatch.setattr(lakebase.lakebase_identity, "credential",
                        lambda user, host: minted.append(user) or "tok")
    monkeypatch.setattr(lakebase.psycopg, "connect", lambda **kw: _PgConn())
    conn = LakebaseConnection(host="h", database="tgt", user="me@x.com", password="",
                              auth_mode=lakebase.lakebase_identity.AUTH_DATABRICKS)
    with conn.session() as s:
        for _ in range(3):
            s.query("SELECT 1")
    assert minted == ["me@x.com"]

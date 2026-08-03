"""Transient-error retry in the Azure SQL connector.

Serverless Azure SQL rejects the first connection after auto-pause with error
40613 while the database resumes; the connector must retry those (and only
those) instead of failing the whole scan.
"""
import pymssql
import pytest

from backend.connectors import azure_sql
from backend.connectors.azure_sql import AzureSqlConnection, _transient_reason


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


@pytest.fixture
def sleeps(monkeypatch):
    """Record sleeps instead of waiting."""
    recorded: list[float] = []
    monkeypatch.setattr(azure_sql.time, "sleep", recorded.append)
    return recorded


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
    assert len(attempts) == azure_sql._MAX_ATTEMPTS
    assert sleeps == [5.0, 10.0, 20.0]


def test_transient_detection_by_code():
    assert _transient_reason(_resuming_error()) == "error 40613"
    for code in sorted(azure_sql._TRANSIENT_CODES):
        assert _transient_reason(pymssql.OperationalError((code, b"busy"))) is not None


def test_transient_detection_by_message_marker():
    # FreeTDS sometimes layers DB-Lib noise around the real message with a
    # non-transient leading code — the wording still identifies the resume.
    exc = pymssql.OperationalError(
        (20018, b"General SQL Server error: Database 'db' is not currently available.")
    )
    assert _transient_reason(exc) is not None


def test_non_pymssql_errors_are_not_transient():
    assert _transient_reason(ValueError("40613")) is None
    assert _transient_reason(pymssql.OperationalError((4060, b"Cannot open database"))) is None

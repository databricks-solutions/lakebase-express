"""Azure SQL source connector.

Reads metadata from Azure SQL using ``pymssql`` (pure-Python, bundles FreeTDS —
no system ODBC driver, works on Python 3.13 and inside the app container). The
assessment scan only pulls small catalog result sets, so no Spark is involved;
Spark is reserved for the generated data-migration code that runs *on Databricks*.

Credentials: the UI supplies host/db/port + the *secret key* of the password,
which we resolve from a Databricks secret scope. The password never transits the
API as plaintext beyond the initial (TLS-protected) store step.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass

import pymssql

log = logging.getLogger("lakebase_express.azure_sql")

# Azure SQL transient error codes that a short retry resolves. The big one for
# serverless tiers is 40613 ("Database ... is not currently available"): the
# first connection after auto-pause lands while the database is still resuming
# and is rejected, but the resume completes within ~30-60s.
_TRANSIENT_CODES = {40197, 40501, 40613, 49918, 49919, 49920}
# pymssql/FreeTDS sometimes buries the code inside a layered DB-Lib message —
# fall back to matching the resume wording itself.
_TRANSIENT_MARKERS = ("is not currently available", "is currently unavailable")

_MAX_ATTEMPTS = 4
_BACKOFF_SECONDS = (5.0, 10.0, 20.0)  # waits between attempts 1→2, 2→3, 3→4


def _transient_reason(exc: BaseException) -> str | None:
    """Return a short description if ``exc`` is a transient Azure SQL error, else None."""
    if not isinstance(exc, pymssql.Error):
        return None
    args = exc.args[0] if exc.args else None
    message = str(exc)
    if isinstance(args, tuple) and args and isinstance(args[0], int):
        if args[0] in _TRANSIENT_CODES:
            return f"error {args[0]}"
        message = " ".join(str(a) for a in args)
    lowered = message.lower()
    for marker in _TRANSIENT_MARKERS:
        if marker in lowered:
            return f"transient message ({marker!r})"
    return None


@dataclass(frozen=True)
class AzureSqlConnection:
    """Connection coordinates for an Azure SQL database.

    The password is supplied directly and used only for the live scan; it is not
    persisted. (The generated migration notebooks read from a secret scope.)
    """

    host: str
    database: str
    username: str
    password: str
    port: int = 1433

    # Default per-query timeout (seconds). Catalog scans are quick; a caller can
    # raise it for a specific query (e.g. an exact COUNT(*) on a huge table).
    _DEFAULT_QUERY_TIMEOUT = 120

    def _connect_once(self, timeout: int = _DEFAULT_QUERY_TIMEOUT):
        return pymssql.connect(
            server=self.host,
            port=str(self.port),
            user=self.username,
            password=self.password,
            database=self.database,
            login_timeout=30,
            timeout=timeout,
        )

    @contextmanager
    def _connect(self, timeout: int = _DEFAULT_QUERY_TIMEOUT):
        # Retry only transient failures (serverless auto-pause resume, service
        # busy). Anything else — bad credentials, missing database, firewall —
        # raises immediately.
        conn = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                conn = self._connect_once(timeout)
                break
            except pymssql.Error as exc:
                reason = _transient_reason(exc)
                if reason is None or attempt == _MAX_ATTEMPTS:
                    raise
                delay = _BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)]
                log.warning(
                    "Transient Azure SQL error on %s/%s (%s) — retrying in %.0fs (attempt %d/%d)",
                    self.host, self.database, reason, delay, attempt, _MAX_ATTEMPTS,
                )
                time.sleep(delay)
        try:
            yield conn
        finally:
            conn.close()

    def query(self, sql: str, timeout: int | None = None) -> list[dict]:
        """Run a read-only query and return rows as a list of dicts.

        Column names are the dict keys, matching how scanner.py reads results.
        ``timeout`` overrides the per-query timeout in seconds — raise it for a
        long exact ``COUNT(*)`` so it isn't cut short mid-scan.
        """
        with self._connect(timeout or self._DEFAULT_QUERY_TIMEOUT) as conn:
            cursor = conn.cursor(as_dict=True)
            cursor.execute(sql)
            return cursor.fetchall()

    @contextmanager
    def read_cursor(self, sql: str):
        """Yield a tuple-returning cursor for streaming extraction.

        Used by the data loader to ``fetchmany`` in batches instead of loading
        the whole table into memory. Column names are on ``cursor.description``.
        """
        with self._connect() as conn:
            cursor = conn.cursor()  # tuple rows, preserves column order
            cursor.execute(sql)
            yield cursor

    def test_connection(self) -> bool:
        """Lightweight connectivity probe used by the UI before a full scan."""
        rows = self.query("SELECT 1 AS ok")
        return bool(rows) and rows[0]["ok"] == 1

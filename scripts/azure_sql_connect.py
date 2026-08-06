"""Standalone probe: connect to Azure SQL exactly the way the app does.

Drives the real connection module (``connectors/factory.build_connector`` ->
``AzureSqlConnection``) outside FastAPI, so a failing "Test connection" in the UI
can be reproduced from a terminal: same driver (pymssql), same login/query
timeouts, same transient-error retry (serverless auto-pause resume, 40613).

Config comes from the environment so nothing workspace-specific is committed:

    export LBX_SRC_HOST="<server>.database.windows.net"
    export LBX_SRC_DATABASE="<db>"
    export LBX_SRC_USER="<login>"
    export LBX_SRC_PASSWORD="<password>"        # or use --secret-scope/--secret-key
    PYTHONPATH=. python3 scripts/azure_sql_connect.py

For local debugging, the probe can load those variables itself.  This avoids
depending on an editor's ``envFile`` parser:

    PYTHONPATH=. python3 scripts/azure_sql_connect.py \
        --env-file .vscode/azure_sql.env

Optional:
    LBX_SRC_PORT=1433               # default 1433
    LBX_SRC_TYPE=azure-sql          # or sql-server (same T-SQL driver)

Instead of a plaintext password, resolve it from a Databricks secret scope the
same way the app's SecretRef path does (bare password or full connection string):

    PYTHONPATH=. python3 scripts/azure_sql_connect.py \
        --secret-scope lakebase-express --secret-key azure-sql-password

To exercise the flow with no server reachable (offline demo of the retry and
query path), pass --simulate: the pymssql driver is replaced with a fake that
fails the first connect with a transient 40613 and then succeeds.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from backend.connectors.factory import build_connector

log = logging.getLogger("lakebase_express.azure_sql_probe")

print("Starting Azure SQL Connect")
# Small catalog queries — the same kind the assessment scan issues, kept cheap so
# the probe is safe to run against production.
_PROBE_QUERIES = (
    ("server", "SELECT @@VERSION AS version"),
    ("user tables", """
        SELECT COUNT(*) AS n
        FROM   sys.tables t
        JOIN   sys.schemas s ON s.schema_id = t.schema_id
        WHERE  t.is_ms_shipped = 0 AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
    """),
)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set — see this script's docstring for the full list.")
    return value


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _load_env_file(file_name: str) -> tuple[str, ...]:
    """Load a small dotenv-style file into ``os.environ``.

    The explicitly supplied file wins over inherited values.  Values are kept
    verbatim apart from surrounding whitespace and one matching pair of outer
    quotes; notably, ``#`` and additional ``=`` characters remain part of a
    password.  Comment lines are skipped before looking at their contents, so
    quotes or equals signs in prose cannot confuse the parser.
    """
    path = Path(file_name).expanduser()
    try:
        contents = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SystemExit(f"Could not read env file {path}: {exc}") from exc

    loaded: set[str] = set()
    for line_number, original in enumerate(contents.splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise SystemExit(f"Invalid env file entry at {path}:{line_number}: expected NAME=VALUE")

        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not _ENV_NAME.fullmatch(name):
            raise SystemExit(f"Invalid environment variable name at {path}:{line_number}: {name!r}")

        value = raw_value.strip()
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise SystemExit(f"Unclosed quoted value at {path}:{line_number} for {name}")
            value = value[1:-1]
        elif value[-1:] in {"'", '"'}:
            raise SystemExit(f"Unmatched closing quote at {path}:{line_number} for {name}")

        os.environ[name] = value
        loaded.add(name)

    return tuple(sorted(loaded))


def _password_from_secret(scope: str, key: str) -> str:
    """Resolve the password through the app's SecretRef path (Databricks Secrets
    API, incl. Key Vault-backed scopes on Azure)."""
    from backend.assessment.models import SecretRef
    from backend.connectors.credentials import resolve_secret_ref

    password = resolve_secret_ref(SecretRef(scope=scope, key=key))
    if not password:
        raise SystemExit(f"Could not resolve a password from secret {scope}/{key} — see the log above.")
    return password


def _install_fake_driver() -> None:
    """Point the connector at a fake driver for --simulate runs.

    The first attempt raises the serverless resume error (40613) so the
    connector's retry path is visible; the second returns a cursor that answers
    the probe queries. Backoff sleeps are skipped.

    Shims replace the connector module's own ``pymssql``/``time`` references
    rather than mutating those modules, so nothing else in the process is
    affected. The real exception classes are kept so ``_transient_reason``'s
    isinstance check still recognizes the simulated error.
    """
    import types

    import pymssql

    from backend.connectors import azure_sql

    attempts = {"n": 0}
    rows = {"version": "Microsoft SQL Azure (simulated) 12.0.2000.8", "n": 42, "ok": 1}

    class _Cursor:
        def __init__(self) -> None:
            self._sql = ""

        def execute(self, sql: str) -> None:
            self._sql = sql

        def fetchall(self) -> list[dict]:
            # Answer whichever alias the probe asked for.
            for alias in ("version", "n", "ok"):
                if alias in self._sql.lower():
                    return [{alias: rows[alias]}]
            return []

    class _Conn:
        def cursor(self, as_dict: bool = False):
            return _Cursor()

        def close(self) -> None:
            pass

    def connect(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise pymssql.OperationalError(
                (40613, b"Database 'db' on server 'srv' is not currently available. "
                        b"Please retry the connection later.")
            )
        log.info("simulated connect: %s@%s/%s", kwargs.get("user"), kwargs.get("server"),
                 kwargs.get("database"))
        return _Conn()

    fake_driver = types.SimpleNamespace(connect=connect, Error=pymssql.Error)
    fake_time = types.SimpleNamespace(
        sleep=lambda seconds: log.info("simulated wait of %.0fs", seconds)
    )
    azure_sql.pymssql = fake_driver
    azure_sql.time = fake_time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--secret-scope", help="Databricks secret scope holding the password.")
    parser.add_argument("--secret-key", help="Secret key inside --secret-scope.")
    parser.add_argument("--env-file",
                        help="Load environment variables from this NAME=VALUE file.")
    parser.add_argument("--simulate", action="store_true",
                        help="Run against a fake driver (no server needed).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    if args.env_file:
        loaded = _load_env_file(args.env_file)
        probe_names = tuple(name for name in loaded if name.startswith("LBX_SRC_"))
        names = ", ".join(probe_names) if probe_names else "no LBX_SRC_* variables"
        print(f"Loaded env file {Path(args.env_file).expanduser()} ({names})", flush=True)

    if args.simulate:
        _install_fake_driver()
        # Real coordinates are used when the environment supplies them, so the
        # printed line and the connector kwargs match what a live run would see
        # (handy for checking an env file is wired up). Nothing is dialled — the
        # fake driver answers — so placeholders cover the unset case.
        host = os.getenv("LBX_SRC_HOST") or "srv.database.windows.net"
        database = os.getenv("LBX_SRC_DATABASE") or "db"
        username = os.getenv("LBX_SRC_USER") or "u@srv"
        password = os.getenv("LBX_SRC_PASSWORD") or "pw"
        port = int(os.getenv("LBX_SRC_PORT") or 1433)
        source_type = os.getenv("LBX_SRC_TYPE") or "azure-sql"
    else:
        if bool(args.secret_scope) != bool(args.secret_key):
            raise SystemExit("--secret-scope and --secret-key must be given together.")
        host = _required("LBX_SRC_HOST")
        database = _required("LBX_SRC_DATABASE")
        username = _required("LBX_SRC_USER")
        password = (_password_from_secret(args.secret_scope, args.secret_key)
                    if args.secret_scope else _required("LBX_SRC_PASSWORD"))
        port = int(os.getenv("LBX_SRC_PORT", "1433"))
        source_type = os.getenv("LBX_SRC_TYPE", "azure-sql")

    # flush: logging writes to stderr, so an unflushed stdout line would print
    # after the connector's retry warnings and confuse the ordering.
    print(f"Connecting to {source_type} {host}:{port}/{database} as {username}"
          + (" [simulated]" if args.simulate else ""), flush=True)

    conn = build_connector(source_type, host=host, database=database, username=username,
                           password=password, port=port)

    try:
        ok = conn.test_connection()
    except Exception as exc:
        # Bad credentials / firewall / missing database land here: the connector
        # retries only transient errors and re-raises everything else.
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"Probe (SELECT 1): {'ok' if ok else 'unexpected result'}")

    for label, sql in _PROBE_QUERIES:
        rows = conn.query(sql)
        value = next(iter(rows[0].values())) if rows else "<no rows>"
        print(f"{label}: {value}")

    print("\nConnection module works against this source.")


if __name__ == "__main__":
    main()

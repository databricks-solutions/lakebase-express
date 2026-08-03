"""Lakebase (Postgres) target connector.

Connects to a Databricks Lakebase database instance over the Postgres wire
protocol using ``psycopg`` and manual role credentials (username + password).
The migration engine uses this to apply DDL/code and bulk-load data.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row


@dataclass(frozen=True)
class LakebaseConnection:
    host: str
    database: str
    user: str
    password: str
    port: int = 5432
    sslmode: str = "require"  # Lakebase requires TLS

    def connect(self) -> psycopg.Connection:
        """Open a new psycopg connection (caller manages its lifecycle)."""
        return psycopg.connect(
            host=self.host,
            dbname=self.database,
            user=self.user,
            password=self.password,
            port=self.port,
            sslmode=self.sslmode,
            connect_timeout=15,
            application_name="lakebase-express",
        )

    def query(
        self, sql: str, params: dict | None = None, statement_timeout_ms: int | None = None
    ) -> list[dict]:
        """Run a read-only query and return rows as dicts keyed by column name.

        Mirrors ``AzureSqlConnection.query`` so catalog/validation code can treat
        both sides of a migration uniformly. ``statement_timeout_ms`` raises the
        session ``statement_timeout`` for this query — used so a long exact
        ``COUNT(*)`` over a huge table isn't cut short by the server default.
        """
        with self.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            if statement_timeout_ms is not None:
                # SET is a utility statement — can't be parameterized, so inline
                # the value (int-cast, no injection surface).
                cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
            cur.execute(sql, params)
            return cur.fetchall()

    def execute(self, sql: str, params: dict | None = None) -> None:
        """Run a statement for its side effects (e.g. ``ANALYZE``); no rows returned."""
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)

    def test_connection(self) -> bool:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
            return bool(row) and row[0] == 1

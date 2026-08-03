"""Streams table data from the source into Lakebase using Postgres COPY.

Extracts in batches (``fetchmany``) so memory stays bounded on large tables, and
bulk-loads via ``COPY ... FROM STDIN`` for speed. A progress callback reports
rows copied so the UI can show live per-table progress.

Type coercion is intentionally light: SQL Server ``bit`` -> Postgres ``boolean``
is handled (driver returns 0/1), and driver-hostile types (``sql_variant``,
``hierarchyid``, spatial) are converted to text server-side in the SELECT list;
other types rely on psycopg's adapters. Tables that hit a coercion edge are
reported as failed with the DB error, not silently skipped.

Constraints created by the plan's post-data phase are handled around the load:
foreign keys touching the target tables are captured and dropped up front (a
re-sync would otherwise fail the TRUNCATE and pay per-row FK validation) and
restored afterwards, and user triggers are disabled during each table's COPY so
translated triggers don't fire once per bulk-loaded row.
"""
from __future__ import annotations

import logging
from typing import Callable

from backend.connectors.azure_sql import AzureSqlConnection
from backend.connectors.lakebase import LakebaseConnection
from backend.migration.models import TableLoadSpec
from backend.schema_migration.naming import map_object, map_schema
from backend.schema_migration.naming import IdentifierCase

log = logging.getLogger("lakebase_express.data_loader")

ProgressFn = Callable[[int], None]

# (table regclass text, constraint name, full definition) of a dropped FK.
DroppedFk = tuple[str, str, str]


def capture_and_drop_fks(target: LakebaseConnection, fq_tables: list[str]) -> list[DroppedFk]:
    """Drop every FK that involves (either side) one of the target tables,
    returning enough to recreate each one. Keeps TRUNCATE working on re-syncs
    and removes per-row FK validation from the COPY hot path; the plan's
    post-data FK items (or restore_fks for data-only runs) put them back."""
    pg = target.connect()
    try:
        with pg.cursor() as cur:
            oids: list[int] = []
            for fq in fq_tables:
                cur.execute("SELECT to_regclass(%s)::oid", (fq,))
                oid = cur.fetchone()[0]
                if oid is not None:
                    oids.append(oid)
            if not oids:
                return []
            cur.execute(
                """
                SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE contype = 'f' AND (conrelid = ANY(%s) OR confrelid = ANY(%s))
                ORDER BY conrelid::regclass::text, conname
                """,
                (oids, oids),
            )
            dropped: list[DroppedFk] = [tuple(r) for r in cur.fetchall()]
            for tbl, name, _ in dropped:
                cur.execute(f'ALTER TABLE {tbl} DROP CONSTRAINT "{name}"')
        pg.commit()
        return dropped
    finally:
        pg.close()


def restore_fks(target: LakebaseConnection, dropped: list[DroppedFk]) -> list[str]:
    """Recreate previously dropped FKs; returns an error string per FK that
    failed (e.g. the load left orphan rows), leaving the rest restored."""
    if not dropped:
        return []
    failures: list[str] = []
    pg = target.connect()
    try:
        for tbl, name, definition in dropped:
            try:
                with pg.cursor() as cur:
                    cur.execute(f'ALTER TABLE {tbl} ADD CONSTRAINT "{name}" {definition}')
                pg.commit()
            except Exception as exc:
                pg.rollback()
                log.warning("FK restore failed for %s on %s: %s", name, tbl, exc)
                failures.append(f"{tbl} {name}: {exc}")
        return failures
    finally:
        pg.close()


def _bit_indices(columns, col_names: list[str]) -> set[int]:
    """Indices (in result order) of source columns typed ``bit``."""
    bit_names = {c.name.lower() for c in columns if c.data_type.lower() == "bit"}
    return {i for i, name in enumerate(col_names) if name.lower() in bit_names}


# SQL Server types the extraction drivers can't fetch raw (pymssql here, Spark's
# JDBC reader in async mode fails with UNRECOGNIZED_SQL_TYPE). Each is projected
# through a server-side conversion to text — matching the ``text`` columns the
# schema plan's type mapper creates for them.
_CAST_SELECT: dict[str, str] = {
    "sql_variant": "CAST({c} AS NVARCHAR(MAX)) AS {a}",
    "hierarchyid": "{c}.ToString() AS {a}",
    "geography": "{c}.STAsText() AS {a}",
    "geometry": "{c}.STAsText() AS {a}",
}


def _source_select(columns) -> str:
    """Explicit SELECT list for the source read, casting exotic types to text.

    Aliases keep the original column names so ``cursor.description`` (and the
    COPY column list built from it) still matches the plan-created table.
    Falls back to ``*`` when column metadata is unavailable.
    """
    if not columns:
        return "*"
    parts = []
    for c in columns:
        col = f"[{c.name}]"
        tmpl = _CAST_SELECT.get(c.data_type.lower())
        parts.append(tmpl.format(c=col, a=col) if tmpl else col)
    return ", ".join(parts)


def load_table(
    source: AzureSqlConnection,
    target: LakebaseConnection,
    spec: TableLoadSpec,
    target_schema: str,
    truncate_first: bool,
    batch_size: int,
    on_progress: ProgressFn,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> int:
    """Load one table; returns total rows copied. Raises on failure.

    ``target_schema`` is where the source ``dbo`` schema lands; each table is
    written to its own mapped schema so the source namespaces are preserved.
    """
    target_table = map_object(spec.target_table or spec.table_name, identifier_case)
    dst_schema = map_schema(spec.schema_name, target_schema, identifier_case)
    src_sql = f'SELECT {_source_select(spec.columns)} FROM [{spec.schema_name}].[{spec.table_name}]'
    fq_target = f'"{dst_schema}"."{target_table}"'

    pg = target.connect()
    pg.autocommit = False
    copied = 0
    try:
        with source.read_cursor(src_sql) as cur:
            col_names = [d[0] for d in cur.description]
            bit_idx = _bit_indices(spec.columns, col_names)
            collist = ", ".join(f'"{c}"' for c in col_names)

            with pg.cursor() as pgcur:
                # Keep translated triggers from firing once per bulk-loaded row.
                # Only touched when the table actually has user triggers (the
                # ALTER needs table ownership); same transaction, so a failed
                # load rolls the trigger state back too.
                pgcur.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_trigger "
                    "WHERE tgrelid = %s::regclass AND NOT tgisinternal)",
                    (fq_target,),
                )
                has_triggers = pgcur.fetchone()[0]
                if has_triggers:
                    pgcur.execute(f"ALTER TABLE {fq_target} DISABLE TRIGGER USER")

                if truncate_first:
                    pgcur.execute(f"TRUNCATE TABLE {fq_target}")

                with pgcur.copy(f"COPY {fq_target} ({collist}) FROM STDIN") as copy:
                    while True:
                        batch = cur.fetchmany(batch_size)
                        if not batch:
                            break
                        for row in batch:
                            if bit_idx:
                                row = tuple(
                                    (bool(v) if (i in bit_idx and v is not None) else v)
                                    for i, v in enumerate(row)
                                )
                            copy.write_row(row)
                        copied += len(batch)
                        on_progress(copied)

                if has_triggers:
                    pgcur.execute(f"ALTER TABLE {fq_target} ENABLE TRIGGER USER")
        pg.commit()
        return copied
    except Exception:
        pg.rollback()
        raise
    finally:
        pg.close()

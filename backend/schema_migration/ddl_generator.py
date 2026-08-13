"""Generates Postgres DDL from scanned Azure SQL table metadata.

Emits a runnable script: schema creation, the collations the source columns use,
then CREATE TABLE per table with mapped types, collations, and nullability.
Identifiers are double-quoted to preserve casing and neutralise T-SQL [bracket]
quoting.

Constraints and indexes (PKs, FKs, unique/plain indexes, column defaults,
check constraints, identity columns) are generated as **post-data** DDL: they
are intentionally created only AFTER the data load, so the bulk COPY doesn't
pay per-row index maintenance and FK validation costs, and so identity
sequences can be synced to MAX(column)+1 once the rows are in. The statements
are idempotent (conditional DO blocks / IF NOT EXISTS) so re-running a
migration converges instead of failing on already-existing objects.
"""
from __future__ import annotations

from backend.assessment.models import (
    CheckConstraintInfo,
    ColumnDefaultInfo,
    ColumnInfo,
    ForeignKeyInfo,
    IndexInfo,
    TableInfo,
)
from backend.schema_migration.collation_mapper import (
    collect_collations,
    column_collation,
)
from backend.schema_migration.expr_mapper import (
    map_default_expression,
    map_expression,
    sequence_ref_in,
)
from backend.schema_migration.naming import (
    IdentifierCase,
    index_name,
    map_object,
    map_schema,
    mapped_identifier,
    primary_key_name,
)
from backend.schema_migration.type_mapper import map_type

# Constraint/index naming lives in schema_migration/naming.py — the validation
# comparator has to look for exactly the names created here, so the rules are
# shared rather than duplicated.
_ident = mapped_identifier


def _fq(
    schema: str,
    table: str,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> str:
    """Fully-qualified, quoted table reference for an already-mapped schema."""
    return f'"{schema}"."{map_object(table, identifier_case)}"'


def _column_ddl(col, *, indent: str = "    ", collation_schema: str = "") -> str:
    null = "" if col.is_nullable else " NOT NULL"
    # Character columns keep their source collation, so comparisons keep the
    # source's case/accent semantics rather than Postgres's case-sensitive default.
    target = column_collation(col)
    collate = f" COLLATE {target.qualified(collation_schema)}" if target else ""
    # Column names are preserved as scanned so the COPY column list keeps matching.
    return f'{indent}"{col.name}" {map_type(col)}{collate}{null}'


def schema_ddl(schema: str) -> str:
    """CREATE SCHEMA for an already-mapped Postgres schema name."""
    return f'CREATE SCHEMA IF NOT EXISTS "{schema}";'


def collation_ddl(collation, schema: str) -> str:
    """CREATE COLLATION for one migrated collation (idempotent)."""
    return collation.ddl(schema)


def table_ddl(
    table: TableInfo,
    schema: str,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
    collation_schema: str = "",
) -> str:
    """CREATE TABLE for a single table (idempotent). ``schema`` is the resolved
    (already-mapped) Postgres schema; the table name follows ``identifier_case``.
    ``collation_schema`` is where the migration created its collations — passing it
    schema-qualifies each COLLATE so ``search_path`` can't break it."""
    cols = ",\n".join(
        _column_ddl(c, collation_schema=collation_schema) for c in table.columns
    )
    return (
        f'CREATE TABLE IF NOT EXISTS "{schema}".'
        f'"{map_object(table.table_name, identifier_case)}" (\n{cols}\n);'
    )


def _table_ddl(
    table: TableInfo,
    schema: str,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
    collation_schema: str = "",
) -> str:
    return f"-- source: {table.fqn}  ({table.row_count:,} rows)\n" + table_ddl(
        table, schema, identifier_case, collation_schema
    )


# --- Post-data DDL (constraints & indexes, applied AFTER the data load) ----------


def primary_key_ddl(
    table: TableInfo,
    schema: str,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> str:
    """ADD PRIMARY KEY, guarded so a re-run (or an already-keyed table) is a no-op."""
    fq = _fq(schema, table.table_name, identifier_case)
    name = primary_key_name(table.table_name, identifier_case)
    cols = ", ".join(f'"{c}"' for c in table.primary_key)
    return (
        "DO $$\nBEGIN\n"
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM pg_constraint\n"
        f"        WHERE conrelid = '{fq}'::regclass AND contype = 'p'\n"
        "    ) THEN\n"
        f'        ALTER TABLE {fq} ADD CONSTRAINT "{name}" PRIMARY KEY ({cols});\n'
        "    END IF;\n"
        "END $$;"
    )


_FK_ACTIONS = {"CASCADE": "CASCADE", "SET_NULL": "SET NULL", "SET_DEFAULT": "SET DEFAULT"}


def foreign_key_ddl(
    fk: ForeignKeyInfo,
    table_name: str,
    schema: str,
    target_schema: str,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> str:
    """ADD FOREIGN KEY (guarded by constraint name). Referenced tables use the
    same schema mapping as the rest of the plan."""
    fq = _fq(schema, table_name, identifier_case)
    ref_fq = _fq(
        map_schema(fk.ref_schema, target_schema, identifier_case),
        fk.ref_table,
        identifier_case,
    )
    name = _ident(fk.name, identifier_case)
    cols = ", ".join(f'"{c}"' for c in fk.columns)
    ref_cols = ", ".join(f'"{c}"' for c in fk.ref_columns)
    actions = ""
    if fk.on_delete in _FK_ACTIONS:
        actions += f" ON DELETE {_FK_ACTIONS[fk.on_delete]}"
    if fk.on_update in _FK_ACTIONS:
        actions += f" ON UPDATE {_FK_ACTIONS[fk.on_update]}"
    return (
        "DO $$\nBEGIN\n"
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM pg_constraint\n"
        f"        WHERE conrelid = '{fq}'::regclass AND conname = '{name}'\n"
        "    ) THEN\n"
        f'        ALTER TABLE {fq} ADD CONSTRAINT "{name}" FOREIGN KEY ({cols})\n'
        f"            REFERENCES {ref_fq} ({ref_cols}){actions};\n"
        "    END IF;\n"
        "END $$;"
    )


def index_ddl(
    idx: IndexInfo,
    table_name: str,
    schema: str,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
    columns: list[ColumnInfo] | None = None,
) -> str:
    """CREATE (UNIQUE) INDEX IF NOT EXISTS. The name is prefixed with the table
    (source index names are per-table; Postgres index names are per-schema).

    ``columns`` is the table's scanned columns, needed to translate a filter that
    compares a ``bit`` column to 0/1 (see ``expr_mapper.map_expression``)."""
    fq = _fq(schema, table_name, identifier_case)
    name = index_name(idx.name, table_name, identifier_case)
    cols = ", ".join(f'"{c.name}"' + (" DESC" if c.descending else "") for c in idx.columns)
    unique = "UNIQUE " if idx.is_unique else ""
    include_cols = ", ".join(f'"{c}"' for c in idx.include_columns)
    include = f" INCLUDE ({include_cols})" if idx.include_columns else ""
    where = (
        f" WHERE {map_expression(idx.filter_definition, columns=columns)}"
        if idx.filter_definition else ""
    )
    return f'CREATE {unique}INDEX IF NOT EXISTS "{name}" ON {fq} ({cols}){include}{where};'


def column_default_ddl(
    d: ColumnDefaultInfo, table_name: str, schema: str, *,
    column: ColumnInfo | None = None, target_schema: str = "public",
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> str:
    """SET DEFAULT with the mechanically-translated default expression.

    ``column`` (the scanned source column) lets bit defaults land as booleans;
    ``target_schema`` maps the schema of any ``NEXT VALUE FOR`` sequence. When the
    default reads a sequence, a ``CREATE SEQUENCE IF NOT EXISTS`` is emitted first
    so the referenced sequence exists. Naturally idempotent (SET DEFAULT replaces;
    CREATE SEQUENCE is guarded)."""
    fq = _fq(schema, table_name, identifier_case)
    expr = map_default_expression(
        d.definition,
        column=column,
        target_schema=target_schema,
        identifier_case=identifier_case,
    )
    set_default = f'ALTER TABLE {fq} ALTER COLUMN "{d.column}" SET DEFAULT {expr};'

    seq = sequence_ref_in(d.definition, target_schema, identifier_case)
    if seq:
        seq_fq = f'"{seq[0]}"."{seq[1]}"'
        return f"CREATE SEQUENCE IF NOT EXISTS {seq_fq};\n{set_default}"
    return set_default


def check_constraint_ddl(
    chk: CheckConstraintInfo,
    table_name: str,
    schema: str,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
    columns: list[ColumnInfo] | None = None,
) -> str:
    """ADD CONSTRAINT ... CHECK (guarded by constraint name). ``columns`` is the
    table's scanned columns, needed to translate a ``bit`` comparison in the
    predicate (see ``expr_mapper.map_expression``)."""
    fq = _fq(schema, table_name, identifier_case)
    name = _ident(chk.name, identifier_case)
    predicate = map_expression(chk.definition, columns=columns)
    return (
        "DO $$\nBEGIN\n"
        "    IF NOT EXISTS (\n"
        "        SELECT 1 FROM pg_constraint\n"
        f"        WHERE conrelid = '{fq}'::regclass AND conname = '{name}'\n"
        "    ) THEN\n"
        f'        ALTER TABLE {fq} ADD CONSTRAINT "{name}" CHECK ({predicate});\n'
        "    END IF;\n"
        "END $$;"
    )


_IDENTITY_PG_TYPES = ("smallint", "integer", "bigint")


def identity_ddl(
    table: TableInfo,
    schema: str,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> str:
    """Recreate the source IDENTITY column semantics after the load.

    Integer-family columns become real Postgres identity columns; anything else
    (SQL Server allows identity on numeric/decimal) gets a sequence-backed
    DEFAULT. Both variants sync the sequence to MAX(column)+1, which is exactly
    why identity is a post-data step.
    """
    col = table.identity_column or ""
    fq = _fq(schema, table.table_name, identifier_case)
    col_info = next((c for c in table.columns if c.name == col), None)
    pg_type = map_type(col_info) if col_info else ""

    if pg_type in _IDENTITY_PG_TYPES:
        return (
            "DO $$\nBEGIN\n"
            "    IF EXISTS (\n"
            "        SELECT 1 FROM pg_attribute\n"
            f"        WHERE attrelid = '{fq}'::regclass AND attname = '{col}' AND attidentity = ''\n"
            "    ) THEN\n"
            f'        ALTER TABLE {fq} ALTER COLUMN "{col}" ADD GENERATED BY DEFAULT AS IDENTITY;\n'
            "    END IF;\n"
            f"    PERFORM setval(pg_get_serial_sequence('{fq}', '{col}'),\n"
            f'                   (SELECT COALESCE(MAX("{col}"), 0)::bigint + 1 FROM {fq}), false);\n'
            "END $$;"
        )

    # Non-integer identity: a plain sequence + DEFAULT nextval() (identity
    # columns are integer-only in Postgres).
    seq = _ident(
        f"{map_object(table.table_name, identifier_case)}_"
        f"{map_object(col, identifier_case)}_seq",
        identifier_case,
    )
    seq_fq = f'"{schema}"."{seq}"'
    return (
        f'CREATE SEQUENCE IF NOT EXISTS {seq_fq} OWNED BY {fq}."{col}";\n'
        f'ALTER TABLE {fq} ALTER COLUMN "{col}" SET DEFAULT nextval(\'{seq_fq}\');\n'
        f'SELECT setval(\'{seq_fq}\', (SELECT COALESCE(MAX("{col}"), 0)::bigint + 1 FROM {fq}), false);'
    )


def post_data_ddl(
    tables: list[TableInfo],
    target_schema: str = "public",
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> list[tuple[str, str]]:
    """All post-data statements as (label, sql), in safe apply order:
    defaults/identity/PK/checks per table, then indexes, then FKs (which need
    the referenced PKs/unique indexes in place)."""
    out: list[tuple[str, str]] = []
    for t in tables:
        schema = map_schema(t.schema_name, target_schema, identifier_case)
        name = f"{schema}.{map_object(t.table_name, identifier_case)}"
        cols = {c.name: c for c in t.columns}
        for d in t.column_defaults:
            out.append((
                f"{name} · DEFAULT {d.column}",
                column_default_ddl(d, t.table_name, schema,
                                   column=cols.get(d.column), target_schema=target_schema,
                                   identifier_case=identifier_case),
            ))
        if t.identity_column:
            out.append((f"{name} · IDENTITY {t.identity_column}", identity_ddl(t, schema, identifier_case)))
        if t.primary_key:
            out.append((f"{name} · PRIMARY KEY", primary_key_ddl(t, schema, identifier_case)))
        for chk in t.check_constraints:
            out.append((
                f"{name} · CHECK {map_object(chk.name, identifier_case)}",
                check_constraint_ddl(chk, t.table_name, schema, identifier_case, t.columns),
            ))
    for t in tables:
        schema = map_schema(t.schema_name, target_schema, identifier_case)
        name = f"{schema}.{map_object(t.table_name, identifier_case)}"
        for idx in t.indexes:
            out.append((
                f"{name} · INDEX {map_object(idx.name, identifier_case)}",
                index_ddl(idx, t.table_name, schema, identifier_case, t.columns),
            ))
    for t in tables:
        schema = map_schema(t.schema_name, target_schema, identifier_case)
        name = f"{schema}.{map_object(t.table_name, identifier_case)}"
        for fk in t.foreign_keys:
            out.append((
                f"{name} · FK {map_object(fk.name, identifier_case)}",
                foreign_key_ddl(fk, t.table_name, schema, target_schema, identifier_case),
            ))
    return out


def generate_ddl(
    tables: list[TableInfo],
    target_schema: str = "public",
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> tuple[str, int]:
    """Return (script, statement_count).

    ``target_schema`` is where the source ``dbo`` schema lands; every other source
    schema maps according to ``identifier_case``. The script has two
    sections: the pre-data DDL (schemas + collations + tables), then the post-data
    DDL (constraints & indexes) to run AFTER the data load.
    """
    # Collations live in the default target schema, created before their tables.
    # It is included in the schema list even when no table maps to it, since the
    # CREATE COLLATION statements below need it to exist.
    collation_schema = map_schema("dbo", target_schema, identifier_case)
    usage = collect_collations(tables)
    schemas = sorted({map_schema(t.schema_name, target_schema, identifier_case) for t in tables}
                     | ({collation_schema} if usage.created() else set()))
    header = (
        "-- Generated by Lakebase Express — schema migration\n"
        "-- Review before running. Constraints & indexes are in the post-data\n"
        "-- section at the end — run that part only AFTER the data load.\n\n"
        + "".join(f'CREATE SCHEMA IF NOT EXISTS "{s}";\n' for s in schemas)
    )

    created = usage.created()
    if created:
        header += (
            "\n-- Source collations, mirrored so string comparison keeps the source's\n"
            "-- case/accent semantics. Insensitive collations must be nondeterministic\n"
            "-- for equality itself to ignore case — Postgres then rejects LIKE on them.\n"
            + "\n".join(f"{c.ddl(collation_schema)}" for c in created)
            + "\n"
        )

    blocks = [
        _table_ddl(
            t, map_schema(t.schema_name, target_schema, identifier_case), identifier_case,
            collation_schema,
        )
        for t in tables
    ]
    post = post_data_ddl(tables, target_schema, identifier_case)
    script = header + "\n" + "\n\n".join(blocks) + "\n"
    if post:
        script += (
            "\n-- === Post-data phase — run AFTER the data load ===\n"
            "-- Defaults, identity, primary keys, checks, indexes, foreign keys.\n\n"
            + "\n\n".join(f"-- {label}\n{sql}" for label, sql in post)
            + "\n"
        )
    return script, len(blocks) + len(schemas) + len(created) + len(post)

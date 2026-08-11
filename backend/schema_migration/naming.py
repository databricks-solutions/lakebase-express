"""Source (SQL Server) → target (Lakebase/Postgres) name mapping.

SQL Server schemas are namespaces just like PostgreSQL schemas, so we preserve
them 1:1 instead of flattening everything into one schema. Conventions:

  * The source default schema ``dbo`` maps to a configurable default schema
    (``public`` by default) — mirroring how ``dbo``/``public`` are each the
    "unqualified" schema in their engine.
  * By default, every other schema and every object name is lower-cased
    (``SalesLT.Product`` -> ``saleslt.product``), so references need no quoting
    in Postgres (which folds unquoted identifiers to lower case).
  * Projects may instead preserve source casing (``SalesLT.Product`` stays
    ``SalesLT.Product``). Generated SQL always double-quotes identifiers, so the
    case-sensitive Postgres names remain usable by applications that already
    quote their source identifiers.
  * Column names are always preserved as scanned so the data-load COPY column
    list keeps matching the source.
"""
from __future__ import annotations

from enum import Enum

DEFAULT_TARGET_SCHEMA = "public"


class IdentifierCase(str, Enum):
    """How source schema/object identifiers are named in Postgres."""

    LOWERCASE = "lowercase"
    PRESERVE = "preserve"


def _preserves_case(identifier_case: IdentifierCase | str) -> bool:
    return identifier_case == IdentifierCase.PRESERVE or identifier_case == "preserve"


def map_schema(
    source_schema: str,
    default_schema: str = DEFAULT_TARGET_SCHEMA,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> str:
    """Map a source schema to its Postgres schema name.

    ``default_schema`` is where the source default schema ``dbo`` lands (it is the
    project's ``target_schema``). All other schemas keep their source name, with
    casing controlled by ``identifier_case``.
    """
    default = (default_schema or DEFAULT_TARGET_SCHEMA).strip() or DEFAULT_TARGET_SCHEMA
    if not _preserves_case(identifier_case):
        default = default.lower()
    s = (source_schema or "").strip()
    if not s or s.lower() == "dbo":
        return default
    return s if _preserves_case(identifier_case) else s.lower()


def map_object(
    name: str, identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE
) -> str:
    """Map an object name according to the project's identifier-case policy."""
    value = name or ""
    return value if _preserves_case(identifier_case) else value.lower()


# A Postgres trigger is two objects — the trigger plus a companion trigger
# function — where SQL Server has one. The migration names that function
# ``<trigger>_fn`` (emitted by schema_migration/ai_translator); the validation
# comparator uses the same rule to recognize the helper it created rather than
# flag it as "extra in target".
TRIGGER_FN_SUFFIX = "_fn"


def trigger_function_name(
    trigger_name: str, identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE
) -> str:
    """Name of the companion trigger function the migration creates for a trigger."""
    return map_object(trigger_name, identifier_case) + TRIGGER_FN_SUFFIX


# --- Constraint and index names ------------------------------------------------------
#
# Post-data objects (PKs, FKs, checks, indexes) are not all named after their
# source object: a PK gets a derived name, and index names are prefixed with the
# table. The names below are shared by the two callers that MUST agree on them —
# ddl_generator, which creates the objects, and the validation comparator, which
# looks for them in the target. A rule that lived in only one of the two would
# make every constraint report as missing-in-target *and* extra-in-target.

# Postgres truncates identifiers at 63 bytes; generated names are cut explicitly
# so a DO-block existence check (and the comparator) compare the same name
# Postgres actually stores.
MAX_IDENTIFIER = 63


def truncate_identifier(name: str) -> str:
    return name[:MAX_IDENTIFIER]


def mapped_identifier(
    name: str, identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE
) -> str:
    """Map an identifier and truncate it the way Postgres would."""
    return truncate_identifier(map_object(name, identifier_case))


def primary_key_name(
    table_name: str, identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE
) -> str:
    """Derived PK constraint name — SQL Server's own PK name is not reused."""
    return mapped_identifier(f"pk_{map_object(table_name, identifier_case)}", identifier_case)


def index_name(
    source_index_name: str,
    table_name: str,
    identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
) -> str:
    """Index names are prefixed with the table: source index names are unique
    per table, Postgres index names must be unique per schema."""
    return mapped_identifier(
        f"{map_object(table_name, identifier_case)}_{map_object(source_index_name, identifier_case)}",
        identifier_case,
    )

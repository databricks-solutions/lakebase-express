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

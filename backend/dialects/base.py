"""The source-dialect interface: what the schema engine needs from a source SQL
dialect to generate Postgres — column types, scalar expressions, and collations.

The engine (ddl_generator, planner, validation comparator, context bundle) calls
only this interface, so a new source (Oracle, MySQL, …) adds a dialect here and a
connector in ``connectors/factory.py`` without touching the engine.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from backend.schema_migration.naming import IdentifierCase
from backend.schema_migration.pg_collation import (
    CollationUsage,
    TargetCollation,
    collect_collations,
)


class SourceDialect(ABC):
    """How one source SQL dialect maps to Postgres.

    Expression methods follow the fail-visibly policy: translate what is known,
    leave the rest verbatim so it errors at apply time rather than silently
    changing meaning.
    """

    #: Short id used in logs and notes, e.g. ``"tsql"``.
    name: str

    # --- Types ---------------------------------------------------------------

    @abstractmethod
    def map_type(self, col) -> str:
        """Postgres column type for a scanned source column (``ColumnInfo``)."""

    @abstractmethod
    def required_extensions(self, tables) -> list[str]:
        """Postgres extensions the mapped columns of ``tables`` need, sorted."""

    # --- Expressions ---------------------------------------------------------

    @abstractmethod
    def map_expression(self, expr: str, *, columns=None) -> str:
        """Translate a CHECK predicate or filtered-index WHERE clause.
        ``columns`` is the owning table's scanned columns, for type-aware fix-ups."""

    @abstractmethod
    def map_default_expression(
        self, expr: str, *, column=None, target_schema: str = "public",
        identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
    ) -> str:
        """Translate a column DEFAULT. ``column`` is the scanned source column."""

    @abstractmethod
    def sequence_ref_in(
        self, expr: str, target_schema: str = "public",
        identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
    ) -> tuple[str, str] | None:
        """Mapped ``(schema, sequence)`` a DEFAULT reads, so the plan can create
        it first; None if the expression reads no sequence."""

    # --- Collations ----------------------------------------------------------

    @abstractmethod
    def column_collation(self, col) -> TargetCollation | None:
        """Target collation mirroring a column's source collation, or None to
        leave it on the database default."""

    def collect_collations(self, tables) -> CollationUsage:
        """Distinct collations ``tables`` need, and the columns using each."""
        return collect_collations(tables, self.column_collation)

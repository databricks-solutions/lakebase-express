"""T-SQL (Azure SQL / SQL Server) dialect, backed by the schema_migration mappers."""
from __future__ import annotations

from backend.dialects.base import SourceDialect
from backend.schema_migration import collation_mapper, expr_mapper, type_mapper
from backend.schema_migration.naming import IdentifierCase


class TsqlDialect(SourceDialect):
    name = "tsql"

    def map_type(self, col) -> str:
        return type_mapper.map_type(col)

    def required_extensions(self, tables) -> list[str]:
        return type_mapper.required_extensions(tables)

    def map_expression(self, expr: str, *, columns=None) -> str:
        return expr_mapper.map_expression(expr, columns=columns)

    def map_default_expression(
        self, expr: str, *, column=None, target_schema: str = "public",
        identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
    ) -> str:
        return expr_mapper.map_default_expression(
            expr, column=column, target_schema=target_schema,
            identifier_case=identifier_case,
        )

    def sequence_ref_in(
        self, expr: str, target_schema: str = "public",
        identifier_case: IdentifierCase | str = IdentifierCase.LOWERCASE,
    ) -> tuple[str, str] | None:
        return expr_mapper.sequence_ref_in(expr, target_schema, identifier_case)

    def column_collation(self, col):
        return collation_mapper.column_collation(col)


TSQL = TsqlDialect()

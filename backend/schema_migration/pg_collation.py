"""Postgres (ICU) side of collation mirroring, shared by every source dialect.

A dialect parses its own collation names into a :class:`SourceCollation`; this
module turns that strength into an ICU locale and the ``CREATE COLLATION`` DDL.
Case/accent-insensitive collations must be **nondeterministic**, or Postgres
compares equality byte-wise and ``'ana' <> 'ANA'`` (see ``collation_mapper``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Used when the source locale carries no language or isn't known to the dialect.
ROOT_LOCALE = "und"

# Byte-order comparison — what a binary source collation does, and one Postgres
# already ships, so it needs no CREATE COLLATION.
BINARY_COLLATION = "C"

_UPPERCASE_FIRST_KEYWORD = "kf-upper"


@dataclass(frozen=True)
class SourceCollation:
    """A parsed source collation name, in dialect-neutral terms."""

    name: str                       # as scanned, e.g. "SQL_Latin1_General_CP1_CI_AS"
    locale_key: str                 # normalized locale prefix, e.g. "sql_latin1_general"
    case_insensitive: bool = False
    accent_insensitive: bool = False
    binary: bool = False
    uppercase_first: bool = False    # uppercase sorts first
    unmapped_flags: tuple[str, ...] = ()   # recognised, not expressible

    @property
    def strength_label(self) -> str:
        """Comparison semantics in words, for findings and plan notes."""
        if self.binary:
            return "binary (byte-order)"
        case = "case-insensitive" if self.case_insensitive else "case-sensitive"
        accent = "accent-insensitive" if self.accent_insensitive else "accent-sensitive"
        return f"{case}, {accent}"


@dataclass(frozen=True)
class TargetCollation:
    """The Postgres collation a source collation maps to."""

    name: str                       # unqualified Postgres collation name
    locale: str = ""                # ICU locale, "" for a built-in
    deterministic: bool = True
    needs_create: bool = True
    source: SourceCollation | None = None   # carried through for findings/notes
    locale_fallback: bool = False   # source locale unknown; ICU root used instead

    @property
    def is_case_insensitive(self) -> bool:
        return bool(self.source and self.source.case_insensitive)

    def qualified(self, schema: str | None) -> str:
        """Spelling for a COLLATE clause. Built-ins are bare (``C`` is in
        pg_catalog); created ones are schema-qualified so ``search_path`` can't
        break them."""
        if not self.needs_create or not schema:
            return f'"{self.name}"'
        return f'"{schema}"."{self.name}"'

    def ddl(self, schema: str) -> str:
        """Idempotent ``CREATE COLLATION``, or "" for a built-in."""
        if not self.needs_create:
            return ""
        deterministic = "true" if self.deterministic else "false"
        return (
            f'CREATE COLLATION IF NOT EXISTS "{schema}"."{self.name}"\n'
            f"    (provider = icu, locale = '{self.locale}', deterministic = {deterministic});"
        )


def icu_locale(
    base: str, *, case_insensitive: bool, accent_insensitive: bool,
    uppercase_first: bool = False,
) -> str:
    """ICU locale carrying the strength that matches the source flags."""
    if accent_insensitive:
        # level1 ignores accents and case; -kc-true adds case back, i.e. CS_AI.
        keywords = "ks-level1" if case_insensitive else "ks-level1-kc-true"
    else:
        keywords = "ks-level2" if case_insensitive else "ks-level3"

    # No upper/lower ordering to prefer when case is ignored anyway.
    if uppercase_first and not case_insensitive:
        keywords = f"{keywords}-{_UPPERCASE_FIRST_KEYWORD}"

    # The -u- extension may appear only once, so append to a base that has one.
    if "-u-" in base:
        return f"{base}-{keywords}"
    return f"{base}-u-{keywords}"


def target_collation(parsed: SourceCollation, locale_base: str | None) -> TargetCollation:
    """The Postgres collation for a parsed source collation. ``locale_base`` is the
    dialect's ICU tag for the source locale, or None if it has none (ICU root is
    then used with the same strength, and the fallback is flagged)."""
    if parsed.binary:
        return TargetCollation(
            name=BINARY_COLLATION, locale="", deterministic=True,
            needs_create=False, source=parsed,
        )

    locale = icu_locale(
        locale_base or ROOT_LOCALE,
        case_insensitive=parsed.case_insensitive,
        accent_insensitive=parsed.accent_insensitive,
        uppercase_first=parsed.uppercase_first,
    )
    # Nondeterministic is what makes equality itself ignore case/accents; a
    # deterministic collation would fix ordering only.
    deterministic = not (parsed.case_insensitive or parsed.accent_insensitive)

    return TargetCollation(
        name=collation_identifier(parsed.name),
        locale=locale,
        deterministic=deterministic,
        needs_create=True,
        source=parsed,
        locale_fallback=locale_base is None,
    )


def collation_identifier(source_name: str) -> str:
    """Postgres name for a migrated collation: the source name kept verbatim, so
    the target schema shows which source collation a column mirrors. Always
    lower-cased — this identifier is generated rather than carried from a source
    object, so the project's identifier-case policy doesn't apply."""
    from backend.schema_migration.naming import truncate_identifier

    return truncate_identifier((source_name or "").strip().lower())


@dataclass
class CollationUsage:
    """Distinct collations a set of tables needs, and which columns use each.
    Both dicts are keyed by Postgres collation name."""

    collations: dict[str, TargetCollation] = field(default_factory=dict)
    columns: dict[str, list[str]] = field(default_factory=dict)   # "schema.table.column"

    def created(self) -> list[TargetCollation]:
        """Collations needing a CREATE, in a stable order."""
        return [c for _, c in sorted(self.collations.items()) if c.needs_create]


def collect_collations(
    tables, column_collation: Callable[[object], TargetCollation | None]
) -> CollationUsage:
    """Walk scanned tables and gather the collations their columns need, using the
    dialect's ``column_collation``."""
    usage = CollationUsage()
    for t in tables:
        for col in t.columns:
            target = column_collation(col)
            if target is None:
                continue
            usage.collations.setdefault(target.name, target)
            usage.columns.setdefault(target.name, []).append(
                f"{t.schema_name}.{t.table_name}.{col.name}"
            )
    return usage

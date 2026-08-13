"""SQL Server collation -> Postgres (ICU) collation translation.

Postgres's default collation is case-*sensitive*, so dropping a ``..._CI_AS``
collation (the Azure SQL default) silently changes results rather than erroring:
``'ana' = 'ANA'`` flips, ORDER BY reorders, GROUP BY/DISTINCT stop collapsing,
and unique indexes accept pairs the source rejected. Both halves of the source
name are therefore translated — comparison strength and locale:

    CS_AS       <loc>-u-ks-level3            deterministic
    CI_AS       <loc>-u-ks-level2            nondeterministic
    CS_AI       <loc>-u-ks-level1-kc-true    nondeterministic
    CI_AI       <loc>-u-ks-level1            nondeterministic
    _BIN/_BIN2  built-in "C"                 deterministic

Case/accent-insensitive collations must be **nondeterministic**: Postgres
compares equality byte-wise otherwise, so a deterministic "CI" collation would
fix sort order and leave ``'ana' <> 'ANA'``. The documented cost is that pattern
matching (LIKE, regex) is rejected on such columns, which
``assessment/compatibility.check_collations`` reports up front.

An unrecognised *locale* falls back to the ICU root with the strength flags kept
(and the fallback reported) — emitting nothing would resolve to the target's
case-sensitive default, the very failure this module prevents. A name that
cannot be parsed at all is declined instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# SQL Server collation-name suffix flags.
_CASE_FLAGS = {"CI", "CS"}
_ACCENT_FLAGS = {"AI", "AS"}

# SC/UTF8 affect storage, not comparison (Lakebase is UTF-8 throughout). KS/WS/VSS
# do alter comparison, but only for Japanese/CJK; they are recorded rather than
# expressed so the assessment can say what wasn't carried over.
_ENCODING_FLAGS = {"SC", "UTF8"}
_UNMAPPED_COMPARISON_FLAGS = {"KS", "WS", "VSS"}

_BINARY_FLAGS = {"BIN", "BIN2"}

_ALL_FLAGS = (
    _CASE_FLAGS | _ACCENT_FLAGS | _ENCODING_FLAGS | _UNMAPPED_COMPARISON_FLAGS | _BINARY_FLAGS
)

# "Pref" (SQL_Latin1_General_Pref_CP1_CI_AS) sorts uppercase first. It sits
# mid-name rather than among the trailing flags, so it is stripped from the locale
# tokens — left in place it would look like an unknown locale — and re-expressed
# as the ICU -kf-upper keyword.
_PREF_TOKEN = "PREF"
_UPPERCASE_FIRST_KEYWORD = "kf-upper"

# Locale prefix (what remains after the SQL_ prefix, CP<n> code page, version
# digits, and trailing flags) -> ICU language tag, keyed lower-case.
# Latin1_General has no single language, so it maps to the ICU root rather than
# pretending to be en/de/fr — root is what Postgres's own examples use.
_LOCALE_MAP: dict[str, str] = {
    "latin1_general": "und",
    "sql_latin1_general": "und",
    "general": "und",
    # Iberian
    "modern_spanish": "es",
    "traditional_spanish": "es-u-co-trad",
    "portuguese": "pt",
    "catalan": "ca",
    "basque": "eu",
    # Western/Central Europe
    "french": "fr",
    "german_phonebook": "de-u-co-phonebk",
    "italian": "it",
    "dutch": "nl",
    "icelandic": "is",
    "danish_norwegian": "da",
    "finnish_swedish": "sv",
    "estonian": "et",
    "latvian": "lv",
    "lithuanian": "lt",
    "polish": "pl",
    "czech": "cs",
    "slovak": "sk",
    "slovenian": "sl",
    "hungarian": "hu",
    "hungarian_technical": "hu",
    "croatian": "hr",
    "romanian": "ro",
    "albanian": "sq",
    # Cyrillic / Greek / Turkic
    "cyrillic_general": "ru",
    "ukrainian": "uk",
    "macedonian_fyrom": "mk",
    "greek": "el",
    "turkish": "tr",
    "azeri_latin": "az",
    "azeri_cyrillic": "az-Cyrl",
    "uzbek_latin": "uz",
    "kazakh": "kk",
    "georgian_modern_sort": "ka",
    "armenian": "hy",
    # Middle East / South & South-East Asia
    "hebrew": "he",
    "arabic": "ar",
    "persian": "fa",
    "urdu": "ur",
    "thai": "th",
    "vietnamese": "vi",
    "indic_general": "hi",
    "hindi": "hi",
    "bengali": "bn",
    "tamil": "ta",
    "telugu": "te",
    "kannada": "kn",
    "malayalam": "ml",
    "gujarati": "gu",
    "punjabi": "pa",
    "marathi": "mr",
    "nepali": "ne",
    "syriac_general": "syr",
    # East Asia. SQL Server encodes the sort variant in the name (XJIS, Wansung,
    # Stroke, Pinyin); the ICU tag carries it as a -co- collation subtag.
    "japanese": "ja",
    "japanese_xjis": "ja",
    "japanese_bushu_kakusu": "ja-u-co-unihan",
    "korean_wansung": "ko",
    "chinese_prc": "zh-Hans",
    "chinese_prc_stroke": "zh-Hans-u-co-stroke",
    "chinese_simplified_pinyin": "zh-Hans-u-co-pinyin",
    "chinese_simplified_stroke_order": "zh-Hans-u-co-stroke",
    "chinese_taiwan_stroke": "zh-Hant-u-co-stroke",
    "chinese_taiwan_bopomofo": "zh-Hant-u-co-zhuyin",
    "chinese_hongkong_stroke_90": "zh-Hant-u-co-stroke",
    "chinese_traditional_stroke_count": "zh-Hant-u-co-stroke",
    "chinese_traditional_pinyin": "zh-Hant-u-co-pinyin",
}

# Used when the source locale carries no language or isn't in the map above.
ROOT_LOCALE = "und"

# Byte-order comparison — what a _BIN/_BIN2 collation does, and one Postgres
# already ships, so it needs no CREATE COLLATION.
BINARY_COLLATION = "C"


@dataclass(frozen=True)
class SourceCollation:
    """A parsed SQL Server collation name."""

    name: str                       # as scanned, e.g. "SQL_Latin1_General_CP1_CI_AS"
    locale_key: str                 # normalized locale prefix, e.g. "sql_latin1_general"
    case_insensitive: bool = False
    accent_insensitive: bool = False
    binary: bool = False
    uppercase_first: bool = False    # the "Pref" token: uppercase sorts first
    unmapped_flags: tuple[str, ...] = ()   # recognised, not expressible (KS/WS/VSS)

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


# Types that can carry a collation. A real scan reports collation_name only for
# character columns; this also guards hand-built ColumnInfo.
COLLATABLE_TYPES = frozenset(
    {"char", "nchar", "varchar", "nvarchar", "text", "ntext", "sysname"}
)


def parse_collation(name: str) -> SourceCollation | None:
    """Parse a SQL Server collation name into its locale prefix and flags, or None
    if it isn't one. The SQL_ prefix, CP<n> code page, and version digits
    (``_100_``) are dropped — none of them affect comparison."""
    raw = (name or "").strip()
    if not raw:
        return None

    parts = [p for p in raw.split("_") if p]
    if not parts:
        return None

    # Taken from the end, so a locale containing a flag-like word keeps it.
    flags: list[str] = []
    while parts and parts[-1].upper() in _ALL_FLAGS:
        flags.append(parts.pop().upper())
    flags.reverse()

    uppercase_first = any(p.upper() == _PREF_TOKEN for p in parts)
    locale_tokens = [
        p for p in parts
        if not p.isdigit()
        and not (p.upper().startswith("CP") and p[2:].isdigit())
        and p.upper() != _PREF_TOKEN
    ]
    if not locale_tokens:
        return None

    locale_key = "_".join(locale_tokens).lower()
    flag_set = set(flags)

    # No flags and no known locale: not a name we understand, so don't guess.
    if not flag_set and locale_key not in _LOCALE_MAP:
        return None

    return SourceCollation(
        name=raw,
        locale_key=locale_key,
        case_insensitive="CI" in flag_set,
        accent_insensitive="AI" in flag_set,
        binary=bool(flag_set & _BINARY_FLAGS),
        uppercase_first=uppercase_first,
        unmapped_flags=tuple(f for f in flags if f in _UNMAPPED_COMPARISON_FLAGS),
    )


def _icu_locale(
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


def map_collation(name: str) -> TargetCollation | None:
    """Translate a SQL Server collation name to its Postgres equivalent, or None if
    it can't be parsed — the caller then leaves the column on the database default
    and the assessment reports it."""
    parsed = parse_collation(name)
    if parsed is None:
        return None

    if parsed.binary:
        return TargetCollation(
            name=BINARY_COLLATION, locale="", deterministic=True,
            needs_create=False, source=parsed,
        )

    base = _LOCALE_MAP.get(parsed.locale_key)
    fallback = base is None
    locale = _icu_locale(
        base or ROOT_LOCALE,
        case_insensitive=parsed.case_insensitive,
        accent_insensitive=parsed.accent_insensitive,
        uppercase_first=parsed.uppercase_first,
    )
    # Nondeterministic is what makes equality itself ignore case/accents; a
    # deterministic collation would fix ordering only (see module docstring).
    deterministic = not (parsed.case_insensitive or parsed.accent_insensitive)

    return TargetCollation(
        name=collation_identifier(parsed.name),
        locale=locale,
        deterministic=deterministic,
        needs_create=True,
        source=parsed,
        locale_fallback=fallback,
    )


def collation_identifier(source_name: str) -> str:
    """Postgres name for a migrated collation: the source name kept verbatim, so
    the target schema shows which SQL Server collation a column mirrors. Always
    lower-cased — this identifier is generated rather than carried from a source
    object, so the project's identifier-case policy doesn't apply."""
    from backend.schema_migration.naming import truncate_identifier

    return truncate_identifier((source_name or "").strip().lower())


def column_collation(col) -> TargetCollation | None:
    """The target collation for a scanned column, or None to leave it defaulted —
    a non-character column, one scanned before collations were captured, or a name
    that can't be parsed."""
    if getattr(col, "collation_name", None) in (None, ""):
        return None
    if col.data_type.lower() not in COLLATABLE_TYPES:
        return None
    return map_collation(col.collation_name)


@dataclass
class CollationUsage:
    """Distinct collations a set of tables needs, and which columns use each.
    Both dicts are keyed by Postgres collation name."""

    collations: dict[str, TargetCollation] = field(default_factory=dict)
    columns: dict[str, list[str]] = field(default_factory=dict)   # "schema.table.column"

    def created(self) -> list[TargetCollation]:
        """Collations needing a CREATE, in a stable order."""
        return [c for _, c in sorted(self.collations.items()) if c.needs_create]


def collect_collations(tables) -> CollationUsage:
    """Walk scanned tables and gather the collations their columns need."""
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

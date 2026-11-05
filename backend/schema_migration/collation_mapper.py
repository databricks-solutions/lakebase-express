"""SQL Server collation -> Postgres (ICU) collation translation (the T-SQL
dialect's half; the Postgres half is ``pg_collation``).

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

from backend.schema_migration.pg_collation import (  # noqa: F401 — re-exported
    BINARY_COLLATION,
    ROOT_LOCALE,
    CollationUsage,
    SourceCollation,
    TargetCollation,
    collation_identifier,
    target_collation,
)
from backend.schema_migration.pg_collation import (
    collect_collations as _collect_collations,
)

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


def map_collation(name: str) -> TargetCollation | None:
    """Translate a SQL Server collation name to its Postgres equivalent, or None if
    it can't be parsed — the caller then leaves the column on the database default
    and the assessment reports it."""
    parsed = parse_collation(name)
    if parsed is None:
        return None
    return target_collation(parsed, _LOCALE_MAP.get(parsed.locale_key))


def column_collation(col) -> TargetCollation | None:
    """The target collation for a scanned column, or None to leave it defaulted —
    a non-character column, one scanned before collations were captured, or a name
    that can't be parsed."""
    if getattr(col, "collation_name", None) in (None, ""):
        return None
    if col.data_type.lower() not in COLLATABLE_TYPES:
        return None
    return map_collation(col.collation_name)


def collect_collations(tables) -> CollationUsage:
    """Walk scanned tables and gather the collations their columns need."""
    return _collect_collations(tables, column_collation)

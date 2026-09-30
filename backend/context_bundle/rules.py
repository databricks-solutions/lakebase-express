"""Static knowledge the bundle carries — rationale, rewrites, and trades.

Everything here is the same for every migration, so it is authored once instead
of derived per project: the expression and collation mappers are pure functions
that keep no decision log, and their *rationale* is product knowledge rather than
project data. The builder includes a section only when its condition actually
fired (the LIKE trade-off appears only if a nondeterministic collation was
emitted), so nothing here is boilerplate in the output.

Kept next to the mappers it describes, in Python rather than Markdown templates:
the text is short, assembles conditionally, and needs no packaging story.
"""
from __future__ import annotations

import re

from backend.assessment.compatibility import CODE_RULES
from backend.connectors.lakebase import TRANSIENT_SQLSTATES
from backend.context_bundle.models import RewriteRule

# --- Identifiers -----------------------------------------------------------------

QUOTING_LOWERCASE = (
    "Schema and object names were lower-cased. Application SQL that spells them in "
    "mixed case must be updated, but references need no quoting — Postgres folds "
    "unquoted identifiers to lower case. Column names were kept exactly as scanned."
)

QUOTING_PRESERVE = (
    "Source casing was kept, and every generated statement double-quotes its "
    "identifiers. Application SQL MUST quote them too: unquoted `SELECT ModifiedDate` "
    "folds to `modifieddate` and fails with an undefined-column error."
)

TARGET_NOTES = [
    "Postgres schemas map 1:1 from SQL Server schemas; the source `dbo` schema "
    "lands in the target schema named here.",
]

# --- Operational -----------------------------------------------------------------

RETRY_NOTE = (
    "Replace retry logic keyed on SQL Server error numbers (1205 deadlock, 4060, "
    "40197, 40501) with SQLSTATE checks on the codes listed here. A serverless "
    "Lakebase instance can be resuming, so a connection error is normal and worth "
    "retrying with backoff over ~30-60s rather than surfacing to the user."
)

IDENTITY_NOTE = (
    "IDENTITY columns became Postgres identity columns with their sequence synced "
    "to MAX(col)+1 after the load. `SCOPE_IDENTITY()`, `@@IDENTITY` and "
    "`IDENT_CURRENT()` do not exist: use `INSERT ... RETURNING <col>`."
)

OPERATIONAL_NOTES = [
    "Primary keys were renamed to `pk_<table>` — SQL Server's own PK name is not "
    "reused — and every index name was prefixed with its table, because Postgres "
    "index names are unique per schema rather than per table. `names` lists primary "
    "keys and unique indexes, the ones application code can hit through ON CONFLICT "
    "or a unique-violation message; other index and constraint names follow the same "
    "`<table>_<name>` rule plus the casing policy above, so they are not listed "
    "one by one.",
    "Triggers became a Postgres trigger plus a companion function named "
    "`<trigger>_fn`; the trigger still fires on its own, but anything that "
    "inspects the catalog will see two objects where SQL Server had one.",
    "Postgres readers never block writers, so `WITH (NOLOCK)` and similar table "
    "hints have no equivalent and should simply be removed rather than translated.",
]

# Decisions that are deliberate trades, not defects. A downstream agent that
# "fixes" one of these silently changes query results.
DELIBERATE_TRADES = [
    "Collations are mirrored exactly, which means case/accent-insensitive columns "
    "carry *nondeterministic* Postgres collations. That is what keeps `'ana' = "
    "'ANA'`, ORDER BY, GROUP BY/DISTINCT and unique-index collisions behaving as "
    "they did in SQL Server. Do not switch them to deterministic collations, drop "
    "the COLLATE clause, or substitute citext: equality would silently become "
    "case-sensitive again. The accepted cost is that LIKE and regex are rejected "
    "on those columns, which are listed with the other column changes.",
    "Expressions the translator did not recognise were left verbatim so they fail "
    "visibly when applied, rather than being guessed at and silently changing "
    "meaning. A predicate reported as passed through is a prompt to review it, not "
    "evidence the translation was lossy.",
    "Scratch collections (`#temp`, `##temp`, `DECLARE @t TABLE`) were rewritten as CTEs "
    "or PL/pgSQL records/arrays, not as Postgres temp tables — and application code must "
    "not reintroduce them. Lakebase pools connections in transaction mode, which cannot "
    "be changed, and session-held temporary tables are unsupported on that endpoint: one "
    "that outlives its transaction is gone, or belongs to another client's backend. A "
    "collection that genuinely needs a relation is listed in the gaps with what it needs "
    "instead (an UNLOGGED table keyed by a run id); everything else belongs in the "
    "statement that reads it.",
    "Postgres caps timestamp/time precision at microseconds, so `datetime2(7)` "
    "became bare `timestamp`. The 100-nanosecond tail is gone by design — "
    "`timestamp(7)` is rejected outright by Postgres.",
]

# --- Type changes application code can observe ------------------------------------
#
# Keyed by source type, and used as glossary keys on a column rather than copied
# onto it: one paragraph repeated across sixty columns was the single largest
# thing in the bundle. Only types whose change affects how application code reads,
# writes, or compares a value are listed — a rename that changes nothing at a call
# site (tinyint -> smallint, nvarchar -> varchar, text -> text) is left out on
# purpose, so the section stays bounded on a wide database.

TYPE_APP_IMPACT: dict[str, str] = {
    "bit": (
        "Now boolean: comparisons written against 0/1 (`flag = 1`, `flag <> 0`) no "
        "longer type-check. Bind real booleans, or use `IS TRUE` / `IS FALSE`."
    ),
    "uniqueidentifier": (
        "Now uuid: the driver returns UUID objects rather than strings, and GUID "
        "casing/byte order is normalised. Code that string-compares GUIDs must "
        "normalise both sides."
    ),
    "money": (
        "Now numeric(19,4): no currency formatting or `$` literal parsing. Bind and "
        "read decimals, never floats."
    ),
    "smallmoney": "Now numeric(10,4): see `money` — bind decimals, no currency formatting.",
    "datetimeoffset": (
        "Now timestamptz: Postgres stores UTC and renders in the session's TimeZone "
        "rather than preserving the original offset. Set the session TimeZone "
        "explicitly, or read offsets from a separate column."
    ),
    "rowversion": (
        "Now bytea with no auto-versioning: the value never changes on UPDATE. "
        "Optimistic-concurrency checks must move to a trigger or to application "
        "logic (e.g. a version column the app increments)."
    ),
    "timestamp": (
        "SQL Server `timestamp` is rowversion, not a datetime — now bytea, and it no "
        "longer changes on UPDATE. Any optimistic-concurrency check built on it stops "
        "detecting conflicts silently: move the check to a trigger, to a version "
        "column the application increments, or to Postgres's own `xmin`."
    ),
    "image": "Now bytea: a driver-level binary type, not a LOB handle.",
    "xml": (
        "Postgres `xml` has no T-SQL XML methods: `.value()`, `.nodes()`, `.query()` "
        "and `.exist()` must be rewritten with xpath()/xmltable()."
    ),
    "hierarchyid": (
        "No Postgres equivalent — stored as text. Every hierarchy method "
        "(`GetAncestor`, `IsDescendantOf`, `GetLevel`) needs redesigning, e.g. onto "
        "ltree or a recursive CTE."
    ),
    "sql_variant": "No Postgres equivalent — stored as text. Typed reads must be reworked.",
    "geography": "PostGIS is not available in base Lakebase — stored as text; spatial queries need redesigning.",
    "geometry": "PostGIS is not available in base Lakebase — stored as text; spatial queries need redesigning.",
}

# Glossary keys for changes that are not about the source type.
TEXT_FALLBACK = "text_fallback"
NONDETERMINISTIC_COLLATION = "nondeterministic_collation"
COLLATION_LOCALE_FALLBACK = "collation_locale_fallback"

_OTHER_CHANGES: dict[str, str] = {
    TEXT_FALLBACK: (
        "The source type has no faithful Postgres mapping and became `text`, so the "
        "value is now a string. Reads, writes and comparisons all need review."
    ),
    NONDETERMINISTIC_COLLATION: (
        "The column carries a nondeterministic collation (named in `collation`) that "
        "mirrors the source's case/accent-insensitive comparison, and Postgres "
        "rejects LIKE, SIMILAR TO and regex on such columns. Queries that "
        "pattern-match this column fail at runtime — compare on an expression with "
        "an explicit deterministic collation, e.g. `col COLLATE \"C\" LIKE ...`, or "
        "filter in the application. See the deliberate trades for why this is not a "
        "defect."
    ),
    COLLATION_LOCALE_FALLBACK: (
        "The source locale was not recognised, so the ICU root locale was used with "
        "the same comparison strength. Sort order may differ from the source for "
        "language-specific data."
    ),
}


def change_glossary(keys: set[str]) -> dict[str, str]:
    """Explanations for the change keys a bundle actually used, in a stable order."""
    known = {**TYPE_APP_IMPACT, **_OTHER_CHANGES}
    return {k: known[k] for k in sorted(keys) if k in known}

# --- T-SQL in application code ----------------------------------------------------
#
# Crisp construct -> Postgres pairs for the rule engine's findings, keyed by
# rule_id. Anything in CODE_RULES without an entry here falls back to the rule's
# own title/recommendation, so the table stays complete as the rules grow.

_APP_REWRITES: dict[str, tuple[str, str]] = {
    "TOP": ("SELECT TOP n ...", "SELECT ... LIMIT n"),
    "ISNULL": ("ISNULL(a, b)", "COALESCE(a, b)"),
    "GETDATE": ("GETDATE() / SYSDATETIME()", "now() — GETUTCDATE() becomes (now() AT TIME ZONE 'utc')"),
    "NEWID": ("NEWID() / NEWSEQUENTIALID()", "gen_random_uuid()"),
    "SQUARE_BRACKETS": ("[identifier]", '"identifier" — or unquoted if names were lower-cased'),
    "PLUS_CONCAT": ("'a' + b", "'a' || b, or concat('a', b) — `+` on text is an error in Postgres"),
    "IIF_CHOOSE": ("IIF(c, a, b) / CHOOSE(n, ...)", "CASE WHEN c THEN a ELSE b END"),
    "ROWCOUNT": ("@@ROWCOUNT", "the driver's rowcount, or GET DIAGNOSTICS n = ROW_COUNT in PL/pgSQL"),
    "IDENTITY": ("SCOPE_IDENTITY() / @@IDENTITY", "INSERT ... RETURNING <column>"),
    "OUTPUT_CLAUSE": ("OUTPUT INSERTED.*", "RETURNING *"),
    "STRING_FUNCS": (
        "LEN / CHARINDEX / DATEPART / DATEADD / DATEDIFF / STUFF / PATINDEX",
        "length / position / date_part / interval arithmetic / overlay — argument order differs, check each call",
    ),
    "MERGE": ("MERGE INTO ... USING", "INSERT ... ON CONFLICT DO UPDATE (Postgres also supports MERGE from 15)"),
    "TEMP_TABLE": (
        "#temp / ##temp",
        "a CTE, or a PL/pgSQL record/array — NOT a Postgres TEMP TABLE, which the pooled "
        "endpoint does not support (see the deliberate trades)",
    ),
    "TABLE_VARIABLE": (
        "DECLARE @t TABLE",
        "a CTE for a single statement, or an array of a composite type with unnest() across "
        "several — NOT a Postgres TEMP TABLE (see the deliberate trades)",
    ),
    "DYNAMIC_SQL": ("EXEC / sp_executesql", "parameterised SQL from the application, or EXECUTE ... USING in PL/pgSQL"),
    "TRY_CATCH": ("BEGIN TRY / BEGIN CATCH", "the driver's exception handling, or BEGIN ... EXCEPTION WHEN in PL/pgSQL"),
    "RAISERROR": ("RAISERROR / THROW", "RAISE EXCEPTION — the application sees a SQLSTATE, not an error number"),
    "SET_OPTIONS": ("SET NOCOUNT / ANSI_NULLS / QUOTED_IDENTIFIER / XACT_ABORT", "no equivalent — remove these statements"),
    "MAX_LOB": ("varchar(max) / varbinary(max)", "text / bytea"),
    "CURSOR": ("DECLARE ... CURSOR", "set-based SQL, or a PL/pgSQL FOR loop"),
    "LINKED_SERVER": ("server.db.schema.object", "no linked servers — use postgres_fdw or stage the data"),
    "INSERTED_DELETED": ("INSERTED / DELETED pseudo-tables", "NEW / OLD records in a row-level trigger"),
    "COLLATE": ("COLLATE <sql server collation>", "the mirrored Postgres collation — see the column changes and the deliberate trades"),
}

# Constructs the rule engine does not scan for but the migration hit anyway, each
# with the pattern that decides `seen_in_source`.
_EXTRA_REWRITES: list[tuple[str, str, str, str, re.Pattern[str]]] = [
    (
        "CONVERT_CAST",
        "CONVERT([type], x) / CAST(x AS [type])",
        "x::<mapped type> — map the target type first (see the column changes); a bare "
        "char/nchar/decimal cast must not be translated blindly, its default "
        "length differs between the engines",
        "medium",
        re.compile(r"\b(CONVERT|CAST)\s*\(", re.IGNORECASE),
    ),
    (
        "AT_TIME_ZONE_WINDOWS",
        "AT TIME ZONE 'E. South America Standard Time'",
        "AT TIME ZONE 'America/Sao_Paulo' — SQL Server uses Windows registry zone "
        "names, Postgres accepts only IANA names and rejects the Windows spelling "
        "at execution time",
        "high",
        re.compile(r"AT\s+TIME\s+ZONE", re.IGNORECASE),
    ),
    (
        "ISJSON",
        "ISJSON(x) = 1",
        "x IS JSON — the predicate exists on the target's PostgreSQL 17",
        "medium",
        re.compile(r"\bISJSON\s*\(", re.IGNORECASE),
    ),
    (
        "TABLE_HINTS",
        "WITH (NOLOCK) / WITH (ROWLOCK) and other table hints",
        "remove them — Postgres MVCC readers never block writers, and there is no "
        "hint syntax to translate into",
        "medium",
        re.compile(r"WITH\s*\(\s*(NOLOCK|ROWLOCK|READUNCOMMITTED|UPDLOCK|HOLDLOCK)", re.IGNORECASE),
    ),
    (
        "ERROR_NUMBERS",
        "Retry/branch on SQL Server error numbers (1205, 4060, 40197)",
        "branch on SQLSTATE instead — see the retryable SQLSTATEs",
        "medium",
        re.compile(r"\b(ERROR_NUMBER\s*\(|@@ERROR)", re.IGNORECASE),
    ),
]


def rewrite_rules(
    fired: dict[str, list[str]], source_text_hits: set[str]
) -> list[RewriteRule]:
    """The rewrite table for this database.

    ``fired`` maps a CODE_RULES rule_id to the objects it matched (from the
    assessment findings); ``source_text_hits`` holds the ids of extra rules whose
    pattern was found in scanned source text. Rules that did not fire are still
    emitted — application code can use a construct the database objects never did
    — but `seen_in_source` says which ones this database actually proved.
    """
    out: list[RewriteRule] = []
    for rule in CODE_RULES:
        construct, postgres = _APP_REWRITES.get(
            rule.rule_id, (rule.title, rule.recommendation)
        )
        affected = fired.get(rule.rule_id, [])
        out.append(
            RewriteRule(
                tsql=construct,
                postgres=postgres,
                severity=rule.severity.value,
                seen_in_source=bool(affected),
                affected_objects=sorted(set(affected)),
            )
        )
    for rule_id, construct, postgres, severity, _pattern in _EXTRA_REWRITES:
        out.append(
            RewriteRule(
                tsql=construct,
                postgres=postgres,
                severity=severity,
                seen_in_source=rule_id in source_text_hits,
            )
        )
    return out


def extra_rule_hits(texts: list[str]) -> set[str]:
    """Ids of the extra rewrite rules whose construct appears in ``texts``."""
    hits: set[str] = set()
    for rule_id, _c, _p, _s, pattern in _EXTRA_REWRITES:
        if any(pattern.search(t) for t in texts if t):
            hits.add(rule_id)
    return hits


# Residue the expression translator is known to leave verbatim, and what it means
# when it reaches the target. Scanned against *translated* SQL, so a hit is an
# expression that will fail when applied (or already did).
_RESIDUE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bisjson\s*\(", re.IGNORECASE),
     "`isjson(...)` has no Postgres function — rewrite as `x IS JSON`."),
    (re.compile(r"\bconvert\s*\(", re.IGNORECASE),
     "`CONVERT(...)` was not translated — its target type has no faithful Postgres "
     "equivalent. Rewrite as an explicit cast."),
    (re.compile(r"\[[^\]]+\]"),
     "Bracketed identifiers survived — Postgres reads `[x]` as an array subscript, "
     "not a quoted name."),
    (re.compile(r"\b(datepart|dateadd|datediff|charindex|patindex|stuff)\s*\(", re.IGNORECASE),
     "A T-SQL-only scalar function survived; Postgres has no function of that name."),
    (re.compile(r"\bAT\s+TIME\s+ZONE\s+'[^']*(Standard|Daylight)\s+Time'", re.IGNORECASE),
     "A Windows time-zone name survived; Postgres accepts only IANA zone names."),
]


def residue_risk(target_sql: str) -> str:
    """What is still unmapped in a translated expression, or "" when it looks clean."""
    for pattern, message in _RESIDUE:
        if pattern.search(target_sql):
            return message
    return ""


# --- Start here -------------------------------------------------------------------

START_HERE = (
    "This is a migration context bundle from Lakebase Express, describing how one "
    "SQL Server/Azure SQL database changed when it was migrated to Databricks "
    "Lakebase (PostgreSQL). It exists so the *application* that talks to that "
    "database can be migrated without re-deriving or contradicting the decisions "
    "already made.\n\n"
    "Read it in this order:\n"
    "1. `provenance.completeness` — what this bundle cannot vouch for yet.\n"
    "2. `target` — the schema and identifier casing every call site depends on.\n"
    "3. `operational.deliberate_trades` — decisions that are trades, not defects. "
    "Do not 'fix' them.\n"
    "4. `columns` and `names` — the value- and identifier-level changes to apply "
    "across queries, ORM mappings and migrations.\n"
    "5. `callables` — how procedure, function and trigger call sites change.\n"
    "6. `rewrite_rules` — how to translate T-SQL embedded in application code, "
    "consistently with what was done to the schema.\n"
    "7. `gaps` — what did not come across, and must be handled in the application.\n\n"
    "Everything here is a delta: only what changed is listed, so anything absent "
    "round-trips unchanged. Sections are tagged with their provenance in "
    "`sections[]` — treat `ai` as advisory and verify it, and `deterministic` as "
    "fact. No secret values are included."
)

TRANSIENT_SQLSTATE_LIST = sorted(TRANSIENT_SQLSTATES)

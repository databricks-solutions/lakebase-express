"""The catalog queries: valid Postgres, and safe to hand to psycopg with parameters.

The placeholder check exists because this failed in the app rather than in a test. A
`LIKE '%refcursor%'` literal added to the routines query made psycopg reject it with
"only '%s', '%b', '%t' are allowed as placeholders, got '%r'" — these queries carry
named parameters, so psycopg scans the entire string for percent signs, **including
inside SQL comments**, and anything that is not a valid placeholder is a hard error at
execute time. Nothing in the unit tests reaches a real connection, so only a check on
the query text itself can catch it.
"""
import re

import pglast
import pytest

from backend.validation import comparator

# Every catalog query in the module, by name.
CATALOG_SQL = {
    name: value
    for name, value in vars(comparator).items()
    if name.startswith("_PG_") and name.endswith("_SQL") and isinstance(value, str)
}

# psycopg accepts %s / %b / %t, the %(name)s forms of those, and a doubled %% literal.
_NAMED = re.compile(r"%\(\w+\)[sbt]")
_PERCENT = re.compile(r"%(.)")


def test_the_catalog_queries_were_all_discovered():
    """A renamed constant must not silently drop out of these checks."""
    assert len(CATALOG_SQL) >= 8
    assert "_PG_ROUTINES_SQL" in CATALOG_SQL


@pytest.mark.parametrize("name", sorted(CATALOG_SQL))
def test_each_catalog_query_is_valid_postgres(name):
    # Named parameters are not SQL, so stand them in as literals before parsing.
    probe = _NAMED.sub("NULL", CATALOG_SQL[name])
    pglast.parse_sql(probe)


@pytest.mark.parametrize("name", sorted(CATALOG_SQL))
def test_each_catalog_query_has_no_stray_percent(name):
    """Anything psycopg would read as a bad placeholder, wherever it appears."""
    remainder = _NAMED.sub("", CATALOG_SQL[name]).replace("%%", "")
    stray = [m.group(0) for m in _PERCENT.finditer(remainder)]
    assert stray == [], (
        f"{name} contains {stray}; psycopg allows only %s/%b/%t (or %% for a literal "
        "percent) in a query executed with parameters — comments are scanned too"
    )


def test_the_check_would_catch_the_regression_it_was_written_for():
    """Guard the guard: the exact literal that broke the app must still be rejected."""
    broken = "SELECT 1 WHERE pg_get_function_arguments(p.oid) LIKE '%refcursor%'"
    remainder = _NAMED.sub("", broken).replace("%%", "")
    assert [m.group(0) for m in _PERCENT.finditer(remainder)] == ["%r", "%'"]


def test_refcursor_detection_uses_argument_types():
    """The replacement must test the actual argument OIDs, not the rendered text."""
    sql = CATALOG_SQL["_PG_ROUTINES_SQL"]
    statement = re.sub(r"--[^\n]*", "", sql)   # prose may still mention LIKE
    assert "has_refcursor" in statement
    assert "position('refcursor' in" in statement
    assert "pg_get_function_arguments" in statement   # renders INOUT/OUT args too
    assert "LIKE" not in statement
    # A cast this environment cannot execute is not worth the risk of being wrong.
    assert "::oid[]" not in statement

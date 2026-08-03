"""Parse a resolved secret into its connection components.

A password secret can hold either the bare password or a whole DB connection
string. When it is a connection string we pull out the password (for the actual
connect) plus the non-secret coordinates (host / database / port) so the UI can
fill those fields too. Anything that is not recognizably a connection string is
treated as a bare password — including passwords that merely contain ``=`` (e.g.
base64 padding), which is why detection keys off recognized field names or a URI
scheme rather than the mere presence of ``=``.

Formats handled: URI, JDBC (SQL Server and Postgres), ADO.NET/ODBC keyword
strings, and libpq keyword strings.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

_HOST_KEYS = {"server", "host", "data source", "datasource", "addr", "address", "network address"}
_DB_KEYS = {"database", "initial catalog", "dbname", "db"}
_PORT_KEYS = {"port"}
_USER_KEYS = {"user id", "uid", "user", "username"}
_PASSWORD_KEYS = {"password", "pwd"}
_SSLMODE_KEYS = {"sslmode", "ssl mode"}

_URI_SCHEMES = ("postgres://", "postgresql://", "jdbc:")


@dataclass
class ParsedSecret:
    """The password to authenticate with, plus any non-secret coordinates found.
    ``is_connection_string`` is False for a bare password (host/etc. are None)."""

    password: str | None
    host: str | None = None
    database: str | None = None
    port: int | None = None
    username: str | None = None
    sslmode: str | None = None
    is_connection_string: bool = False


def parse_secret_value(value: str) -> ParsedSecret:
    """Parse a resolved secret value. Never raises — an unrecognized value is
    returned as a bare password."""
    v = (value or "").strip()
    if not v:
        return ParsedSecret(password=value)

    lower = v.lower()
    try:
        if lower.startswith(("postgres://", "postgresql://")):
            return _parse_uri(v)
        if lower.startswith("jdbc:"):
            return _parse_jdbc(v[5:])
        if _looks_like_keyword_string(v):
            return _parse_keyword(v)
    except Exception:
        pass
    return ParsedSecret(password=value)


def _parse_uri(uri: str) -> ParsedSecret:
    parts = urlsplit(uri)
    q = parse_qs(parts.query)
    password = unquote(parts.password) if parts.password else _first(q, "password", "pwd")
    username = unquote(parts.username) if parts.username else _first(q, "user", "uid")
    database = parts.path.lstrip("/") or _first(q, "database", "dbname", "db") or None
    return ParsedSecret(
        password=password,
        host=parts.hostname,
        database=database,
        port=parts.port,
        username=username,
        sslmode=_first(q, "sslmode"),
        is_connection_string=True,
    )


def _parse_jdbc(rest: str) -> ParsedSecret:
    """``rest`` is the part after ``jdbc:`` — e.g. ``sqlserver://host:1433;a=b;…``
    or ``postgresql://host:5432/db?a=b``. SQL Server's JDBC uses ``;`` separated
    properties after the authority; Postgres uses a normal ``?`` query string."""
    scheme, _, tail = rest.partition("://")
    if scheme.lower() == "postgresql":
        return _parse_uri("postgresql://" + tail)

    authority, _, props = tail.partition(";")
    host, port = _split_host_port(authority)
    fields = _kv_pairs(props, sep=";")
    return ParsedSecret(
        password=_pick(fields, _PASSWORD_KEYS),
        host=host,
        database=_pick(fields, _DB_KEYS),
        port=_to_int(_pick(fields, _PORT_KEYS)) or port,
        username=_pick(fields, _USER_KEYS),
        sslmode=_pick(fields, _SSLMODE_KEYS),
        is_connection_string=True,
    )


def _looks_like_keyword_string(v: str) -> bool:
    """True if the value carries a recognized connection-string field. Requires a
    named key (not just any ``=``) so a base64/`=`-bearing password isn't mistaken
    for a connection string."""
    fields = _kv_pairs(v, sep=";" if ";" in v else None)
    if not fields:
        return False
    keys = set(fields)
    return bool(keys & (_HOST_KEYS | _DB_KEYS | _USER_KEYS | _PASSWORD_KEYS))


def _parse_keyword(v: str) -> ParsedSecret:
    fields = _kv_pairs(v, sep=";" if ";" in v else None)
    host = _pick(fields, _HOST_KEYS)
    port = _to_int(_pick(fields, _PORT_KEYS))
    if host:
        host, embedded_port = _split_host_port(host)
        port = port or embedded_port
    return ParsedSecret(
        password=_pick(fields, _PASSWORD_KEYS),
        host=host,
        database=_pick(fields, _DB_KEYS),
        port=port,
        username=_pick(fields, _USER_KEYS),
        sslmode=_pick(fields, _SSLMODE_KEYS),
        is_connection_string=True,
    )


def _kv_pairs(text: str, sep: str | None) -> dict[str, str]:
    """Split ``text`` into a {normalized_key: value} dict. ``sep=None`` splits on
    whitespace using libpq-compatible quoting; ``sep=';'`` respects ADO.NET/JDBC
    quotes and ODBC braces so delimiters inside passwords remain part of the
    value."""
    out: dict[str, str] = {}
    tokens = _semicolon_tokens(text) if sep is not None else _libpq_tokens(text)
    for tok in tokens:
        if "=" not in tok:
            continue
        k, _, val = tok.partition("=")
        key = " ".join(k.strip().lower().split())
        if key:
            out[key] = _unquote_value(val)
    return out


def _semicolon_tokens(text: str) -> list[str]:
    """Split a semicolon connection string without splitting inside quoted or
    ODBC-braced values.

    ADO.NET accepts single/double-quoted values (with doubled quote escapes),
    while ODBC and SQL Server JDBC use ``{...}`` with ``}}`` for a literal close
    brace. Keeping the wrappers here lets ``_unquote_value`` decode them after
    tokenization.
    """
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    in_braces = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                if nxt == quote:
                    buf.append(nxt)
                    i += 1
                else:
                    quote = None
        elif in_braces:
            buf.append(ch)
            if ch == "}":
                if nxt == "}":
                    buf.append(nxt)
                    i += 1
                else:
                    in_braces = False
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == "{":
            in_braces = True
            buf.append(ch)
        elif ch == ";":
            tokens.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    tokens.append("".join(buf))
    return tokens


def _libpq_tokens(text: str) -> list[str]:
    """Tokenize libpq ``key=value`` syntax with quoted/escaped whitespace."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _unquote_value(value: str) -> str:
    """Remove the quoting wrapper used by ADO.NET/ODBC values."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        quote = v[0]
        return v[1:-1].replace(quote * 2, quote)
    if len(v) >= 2 and v[0] == "{" and v[-1] == "}":
        return v[1:-1].replace("}}", "}")
    return v


def _pick(fields: dict[str, str], aliases: set[str]) -> str | None:
    for k in aliases:
        if k in fields and fields[k]:
            return fields[k]
    return None


def _first(q: dict[str, list[str]], *names: str) -> str | None:
    for n in names:
        if q.get(n):
            return q[n][0]
    return None


def _split_host_port(value: str) -> tuple[str | None, int | None]:
    """Split ``host:1433`` or ADO.NET ``tcp:host,1433`` into (host, port)."""
    if not value:
        return None, None
    v = value.strip()
    if v.lower().startswith("tcp:"):
        v = v[4:]
    if "," in v:
        host, _, port = v.partition(",")
        return host.strip() or None, _to_int(port)
    if ":" in v:
        host, _, port = v.rpartition(":")
        return host.strip() or None, _to_int(port)
    return v or None, None


def _to_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None

"""Static check that hand-written SQL only names columns the schema declares.

Neither the Grafana dashboards nor librae/db/'s queries run against a real
database in this suite, so a column renamed in timescale_init.sql leaves them
silently broken — a red panel that reads as "no data", or a query that raises
only in production. Both sides check themselves against the reference schema
through these helpers.

Only unambiguous single-table statements can be checked honestly; every other
shape (CTE, join, derived table, unresolved template) is skipped rather than
guessed at, which is why callers assert a floor on how many they covered.
"""

from __future__ import annotations

import re
from pathlib import Path

INIT_SQL = Path(__file__).resolve().parent.parent / "librae" / "db" / "timescale_init.sql"

_CREATE_TABLE = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", re.DOTALL | re.IGNORECASE
)
_COLUMN = re.compile(r"^\s{4}(\w+)\s+\S")
_NOT_A_COLUMN = frozenset({"constraint", "primary", "unique", "foreign", "check"})

# Everything a bare word in a statement can be other than a column reference.
_SQL_KEYWORDS = """
    all and as asc between by case cross current desc distinct else end epoch exists false
    filter following from full group having in inner intersect interval is join lateral left
    like limit not null offset on or order outer over partition preceding right rows select
    then true union unnest using when where window with
"""
SQL_WORDS = frozenset(_SQL_KEYWORDS.split())

_STRING = re.compile(r"'[^']*'")
_PARAM = re.compile(r"%\(\w+\)s|%s")
_LABELLED_OUTPUT = re.compile(r"\bAS\s+\"[^\"]*\"", re.IGNORECASE)
_QUOTED = re.compile(r"\"[^\"]*\"")
_GRAFANA = re.compile(r"\$\{[^}]*\}|\$\w+")
_CAST = re.compile(r"::\s*\w+")
_CALL = re.compile(r"\b[A-Za-z_]\w*\s*\(")
_NAMED_ARG = re.compile(r"\b\w+\s*=>")
_BARE_ALIAS = re.compile(r"\bAS\s+(\w+)", re.IGNORECASE)
# EXTRACT(EPOCH FROM ...) — that FROM introduces no table, and reading it as
# one makes a single-table statement look multi-table and skip itself silently.
_TABLE_REF = re.compile(r"\b(?<!EPOCH\s)(?:FROM|JOIN)\s+(\w+)(?:\s+(\w+))?", re.IGNORECASE)
_DERIVED_TABLE = re.compile(r"\b(?:FROM|JOIN)\s*\(", re.IGNORECASE)
_QUALIFIED = re.compile(r"\b\w+\.(\w+)\b")
_WORD = re.compile(r"\b[A-Za-z_]\w*\b")


def columns_by_table() -> dict[str, set[str]]:
    """Column names of every CREATE TABLE in the reference schema."""
    sql = INIT_SQL.read_text(encoding="utf-8")
    return {
        name: {
            match.group(1)
            for line in body.splitlines()
            if (match := _COLUMN.match(line)) and match.group(1).lower() not in _NOT_A_COLUMN
        }
        for name, body in _CREATE_TABLE.findall(sql)
    }


def strip_non_columns(sql: str) -> str:
    """Drop everything that is not a column or table reference."""
    sql = _STRING.sub(" ", sql)  # literals, including '${run_id}'
    sql = _PARAM.sub(" ", sql)  # psycopg placeholders: %s, %(name)s
    sql = _LABELLED_OUTPUT.sub(" ", sql)  # AS "P&L" — an output label, not a column
    sql = _QUOTED.sub(" ", sql)
    sql = _GRAFANA.sub(" ", sql)  # ${account_id:sqlstring}, $__timeFilter, $n
    sql = _CAST.sub(" ", sql)  # ::numeric
    sql = _CALL.sub("(", sql)  # an identifier before "(" is a function
    sql = _NAMED_ARG.sub(" ", sql)  # make_interval(secs => ...)
    # A bare output alias (AS status) is not a column; a keyword after AS is
    # only ever the remains of a label stripped above, and must survive.
    return _BARE_ALIAS.sub(
        lambda m: " " if m.group(1).lower() not in SQL_WORDS else m.group(0), sql
    )


def single_table(raw_sql: str, schema: dict[str, set[str]]) -> tuple[str, str] | None:
    """(table, stripped SQL) when every bare identifier must be that table's column.

    Requires one known table in FROM/JOIN, no CTE, no nested SELECT, no derived
    table and no unresolved ``str.format`` placeholder. Returns None for every
    other shape rather than guessing which table a bare identifier belongs to.
    """
    # The derived-table test reads the raw text: stripping rewrites every call
    # to a bare "(", which turns EXTRACT(EPOCH FROM NOW()) into a false match.
    if _DERIVED_TABLE.search(raw_sql):
        return None
    sql = strip_non_columns(raw_sql)
    upper = sql.upper()
    if upper.count("SELECT") != 1 or "WITH" in upper:
        return None
    if "{" in sql:  # an unresolved str.format placeholder — not this statement's text
        return None
    refs = _TABLE_REF.findall(sql)
    tables = {table for table, _ in refs}
    if len(tables) != 1 or not tables <= schema.keys():
        return None
    for _, alias in refs:
        if alias and alias.lower() not in SQL_WORDS:
            sql = re.sub(rf"\b{re.escape(alias)}\b", " ", sql)
    return tables.pop(), sql


def unknown_columns(table: str, stripped_sql: str, schema: dict[str, set[str]]) -> list[str]:
    """Identifiers in a single-table statement that the table does not have."""
    return [
        word
        for word in _WORD.findall(_QUALIFIED.sub(r"\1", stripped_sql))
        if word.lower() not in SQL_WORDS and word != table and word not in schema[table]
    ]

"""Every reference-schema table must be read or written by an engine path.

A table belongs in timescale_init.sql only when engine code depends on its
shape; data only the caller reads or writes is the caller's schema (see
architecture.md, "Who owns which table"). This walks the code instead of
trusting a list: a table counts as used when its name follows FROM, JOIN, INTO,
UPDATE or TABLE in SQL that an engine path runs, either directly or through a
librae/db helper it calls.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.sql_schema_check import INIT_SQL

LIBRAE = Path(__file__).resolve().parents[2] / "librae"
DB = LIBRAE / "db"
# Inside librae/db only these run on an engine path; every other module there
# is a helper that counts only when an engine path calls into it.
DB_ENGINE_MODULES = frozenset({"timescale_state.py", "schema.py"})

_TABLE = re.compile(
    r"CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\w+\.)?(\w+)", re.IGNORECASE
)
_SQL_REF = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(\w+)")
# A CTE's own name follows FROM like a table's, so a caller table that shares
# it would otherwise count as read.
_CTE = re.compile(r"\b(\w+)\s+AS\s+(?:NOT\s+)?(?:MATERIALIZED\s+)?\(", re.IGNORECASE)


def _engine_files() -> list[Path]:
    return [
        path
        for path in sorted(LIBRAE.rglob("*.py"))
        if path.parent != DB or path.name in DB_ENGINE_MODULES
    ]


def _docstrings(tree: ast.AST) -> set[int]:
    nodes = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, nodes)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _literals(node: ast.AST, skip: set[int]) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and id(child) not in skip
    ]


def _sql_tables(node: ast.AST, skip: set[int], ctes: set[str]) -> set[str]:
    """Tables named in the SQL text of every string literal under ``node``."""
    return {t for text in _literals(node, skip) for t in _SQL_REF.findall(text)} - ctes


def _ctes(tree: ast.AST, skip: set[int]) -> set[str]:
    """CTE names defined anywhere in one module; its queries are split across literals."""
    return {name for text in _literals(tree, skip) for name in _CTE.findall(text)}


def _names(node: ast.AST) -> set[str]:
    """Every identifier a librae/db definition uses or imports."""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.alias):
            found.add(child.name)
    return found


def _helpers() -> dict[str, tuple[set[str], set[str]]]:
    """Top-level librae/db definitions: name -> (tables in its SQL, names it uses).

    A module-level SQL constant counts toward every definition that names it.
    """
    helpers: dict[str, tuple[set[str], set[str]]] = {}
    for path in sorted(DB.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        ctes = _ctes(tree, skip)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                defined = [(node.name, node, _names(node))]
            elif isinstance(node, ast.Assign):
                defined = [(t.id, node, set()) for t in node.targets if isinstance(t, ast.Name)]
            else:
                continue
            for name, body, uses in defined:
                tables, known_uses = helpers.setdefault(name, (set(), set()))
                tables |= _sql_tables(body, skip, ctes)
                known_uses |= uses
    return helpers


def _db_imports(tree: ast.AST) -> set[str]:
    """Names an engine module imports from librae.db."""
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("librae.db")
        for alias in node.names
    }


def _engine_tables() -> set[str]:
    helpers = _helpers()
    tables: set[str] = set()
    pending: set[str] = set()
    for path in _engine_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        tables |= _sql_tables(tree, skip, _ctes(tree, skip))
        pending |= _db_imports(tree) & helpers.keys()
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        seen.add(name)
        helper_tables, uses = helpers[name]
        tables |= helper_tables
        pending |= (uses & helpers.keys()) - seen
    return tables


def _schema_tables() -> list[str]:
    return _TABLE.findall(INIT_SQL.read_text(encoding="utf-8"))


def test_every_schema_table_has_an_engine_reader_or_writer() -> None:
    unused = sorted(set(_schema_tables()) - _engine_tables())

    assert not unused, (
        f"{unused} in timescale_init.sql has no reader or writer on an engine path. "
        "Librae's schema holds only tables the engine reads or writes; a table only "
        "the caller uses is the caller's to define and migrate (architecture.md, "
        "'Who owns which table'). Move it out, or add the engine path that uses it."
    )


def test_the_walk_sees_the_engine_tables() -> None:
    """A walker that stopped matching SQL would pass the checks above vacuously."""
    assert {"execution_runtime_state", "broker_orders", "librae_schema_revision"} <= (
        _engine_tables()
    )
    assert {"position_events", "ohlcv"} <= _engine_tables()  # via timescale_writer helpers
    assert "positions" not in _engine_tables()  # a dashboard CTE, not a table


def test_every_create_table_spelling_is_checked() -> None:
    sql = "CREATE TABLE a (x INT); create unlogged table IF NOT EXISTS public.b (x INT);"

    assert _TABLE.findall(sql) == ["a", "b"]

"""librae/db/'s own SQL must name columns the reference schema declares.

The suite mocks the connection everywhere, so these statements are never
executed against a real database: a renamed column raises only in production.
position_events.pnl -> realized_pnl broke the Grafana panels (guarded since)
and load_position_events, which failed every refresh_performance call silently.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.sql_schema_check import columns_by_table, single_table, unknown_columns

DB_DIR = Path(__file__).resolve().parents[2] / "librae" / "db"


def _string_literal(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _nodes(scope: ast.AST):
    """One scope's nodes in source order.

    Nested defs are scopes of their own; an f-string is skipped whole, since
    only fragments of its SQL are literal text.
    """
    for child in ast.iter_child_nodes(scope):
        if isinstance(
            child,
            ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda | ast.JoinedStr,
        ):
            continue
        yield child
        yield from _nodes(child)


def _statements(path: Path) -> list[tuple[str, str]]:
    """(where, SQL) for every statement built purely from string literals.

    A query is either a literal handed straight to `cur.execute(...)` or
    `sql = "SELECT ..."` extended by `sql += "..."` for the optional
    predicates — so a literal assignment opens a statement and the literal
    `+=` following it in source order close it out. Concatenating every
    branch overstates no column: a column that exists in one branch exists in
    all. Anything non-literal (an f-string, a `.format()`, a joined list of
    predicates) ends the statement where it is: what came before is still real
    SQL worth checking, and the rest is invisible here.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scopes = [tree, *(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))]
    found = []
    for scope in scopes:
        where = f"{path.name}:{getattr(scope, 'name', '<module>')}"
        open_statements: dict[str, str] = {}
        concatenated: set[int] = set()
        for node in _nodes(scope):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                if target.id in open_statements:
                    found.append((where, open_statements.pop(target.id)))
                if (text := _string_literal(node.value)) is not None:
                    open_statements[target.id] = text
                    concatenated.add(id(node.value))
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
                if name not in open_statements:
                    continue
                if (text := _string_literal(node.value)) is not None:
                    open_statements[name] += text
                    concatenated.add(id(node.value))
                else:
                    found.append((where, open_statements.pop(name)))
            elif (text := _string_literal(node)) is not None and id(node) not in concatenated:
                found.append((where, text))  # an inline cur.execute("SELECT ...")
        found.extend((where, text) for text in open_statements.values())
    return [(where, text) for where, text in found if text.strip().upper().startswith("SELECT")]


def _single_table_statements() -> list[tuple[str, str, str]]:
    """(where, table, stripped SQL) for every unambiguous statement in librae/db/."""
    schema = columns_by_table()
    resolved = []
    for path in sorted(DB_DIR.glob("*.py")):
        for where, sql in _statements(path):
            if (match := single_table(sql, schema)) is not None:
                table, stripped = match
                resolved.append((where, table, stripped))
    return resolved


def test_every_single_table_statement_selects_columns_that_exist() -> None:
    schema = columns_by_table()
    unknown = [
        (where, table, word)
        for where, table, sql in _single_table_statements()
        for word in unknown_columns(table, sql, schema)
    ]

    assert unknown == []


def test_the_check_covers_the_unambiguous_statements() -> None:
    """An extractor that stopped matching would leave nothing to check and
    still pass the assertion above."""
    assert len(_single_table_statements()) >= 15  # 18 of 24 SELECTs when written

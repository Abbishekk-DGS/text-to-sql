"""Static SQL safety guard, built on a real parser (sqlglot), not regexes.

Layer 1 of 2. Layer 2 is the read-only SQLite connection, so even a parser bypass can't write.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp


class UnsafeSQL(ValueError):
    pass


FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Command, exp.Merge, exp.TruncateTable, exp.Pragma, exp.Attach, exp.Detach,
)  # fmt: skip


def check_and_limit(sql: str, max_rows: int = 200, dialect: str = "sqlite") -> str:
    """Return a safe, LIMIT-capped version of the query, or raise UnsafeSQL."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQL(f"could not parse SQL: {e}") from e
    if len(statements) != 1:
        raise UnsafeSQL("exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise UnsafeSQL(f"only SELECT queries are allowed, got {tree.key.upper()}")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN):
            raise UnsafeSQL(f"{type(node).__name__.upper()} is not allowed")

    limit = tree.args.get("limit")
    current = None
    if limit is not None and isinstance(limit.expression, exp.Literal):
        current = int(limit.expression.this)
    if current is None or current > max_rows:
        tree = tree.limit(max_rows)
    return tree.sql(dialect=dialect)

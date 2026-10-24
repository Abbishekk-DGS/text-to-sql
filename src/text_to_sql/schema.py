"""Read a SQLite schema and render it compactly for the prompt, with sample values."""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Column:
    name: str
    type: str
    pk: bool = False
    samples: list[str] = field(default_factory=list)
    is_enum: bool = False


@dataclass
class Table:
    name: str
    columns: list[Column]
    foreign_keys: list[tuple[str, str, str]]  # (column, ref_table, ref_column)
    row_count: int

    def ddl(self) -> str:
        entries: list[tuple[str, str]] = []  # (definition, trailing comment)
        for c in self.columns:
            pk = " PRIMARY KEY" if c.pk else ""
            kind = "one of" if c.is_enum else "e.g."
            hint = f"-- {kind} {', '.join(c.samples)}" if c.samples else ""
            entries.append((f"{c.name} {c.type or 'TEXT'}{pk}", hint))
        for col, ref_t, ref_c in self.foreign_keys:
            entries.append((f"FOREIGN KEY ({col}) REFERENCES {ref_t}({ref_c})", ""))
        lines = []
        for i, (definition, comment) in enumerate(entries):
            sep = "," if i < len(entries) - 1 else ""
            lines.append(f"  {definition}{sep}" + (f"  {comment}" if comment else ""))
        body = "\n".join(lines)
        return f"CREATE TABLE {self.name} (  -- {self.row_count} rows\n{body}\n);"


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """Open SQLite in read-only mode at the driver level: writes fail even if the guard is bypassed."""
    if str(path) == ":memory:":
        return sqlite3.connect(":memory:")
    uri = f"file:{Path(path).resolve()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def load_csvs(paths: list[Path]) -> sqlite3.Connection:
    """Load CSV files into an in-memory database (one table per file), with type sniffing."""
    conn = sqlite3.connect(":memory:")
    for path in paths:
        table = re.sub(r"\W+", "_", path.stem).strip("_").lower() or "data"
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = [
                re.sub(r"\W+", "_", h).strip("_").lower() or f"col{i}"
                for i, h in enumerate(next(reader))
            ]
            rows = list(reader)
        types = [_sniff([r[i] for r in rows[:500] if i < len(r)]) for i in range(len(header))]
        cols = ", ".join(f'"{h}" {t}' for h, t in zip(header, types, strict=True))
        conn.execute(f'CREATE TABLE "{table}" ({cols})')
        conn.executemany(
            f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(header))})',
            [(r + [None] * len(header))[: len(header)] for r in rows],
        )
    conn.commit()
    return conn


def _sniff(values: list[str]) -> str:
    values = [v for v in values if v not in ("", None)]
    if not values:
        return "TEXT"
    for typ, cast in (("INTEGER", int), ("REAL", float)):
        try:
            for v in values:
                cast(v)
            return typ
        except ValueError:
            continue
    return "TEXT"


def introspect(conn: sqlite3.Connection, samples: int = 3, enum_max: int = 12) -> list[Table]:
    tables = []
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for name in names:
        cols = [
            Column(name=r[1], type=r[2], pk=bool(r[5]))
            for r in conn.execute(f'PRAGMA table_info("{name}")')
        ]
        fks = [(r[3], r[2], r[4]) for r in conn.execute(f'PRAGMA foreign_key_list("{name}")')]
        count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        for c in cols:
            if c.type.upper() in ("TEXT", "") or "CHAR" in c.type.upper():
                # Low-cardinality columns (status, category, city) get ALL their values listed,
                # so the model can match "cancelled" exactly instead of guessing.
                rows = conn.execute(
                    f'SELECT DISTINCT "{c.name}" FROM "{name}" WHERE "{c.name}" IS NOT NULL LIMIT ?',
                    (enum_max + 1,),
                ).fetchall()
                c.is_enum = len(rows) <= enum_max
                keep = rows if c.is_enum else rows[:samples]
                c.samples = [repr(str(r[0])[:30]) for r in keep]
        tables.append(Table(name, cols, fks, count))
    return tables


def link_schema(question: str, tables: list[Table], max_tables: int = 6) -> list[Table]:
    """Schema linking: keep the tables whose names, columns or values the question mentions,
    plus their foreign-key neighbours. Keeps prompts small on databases with many tables."""
    if len(tables) <= max_tables:
        return tables
    q = set(re.findall(r"[a-z0-9]+", question.lower()))
    q |= {w.rstrip("s") for w in q}

    def score(t: Table) -> int:
        terms = {t.name.lower(), t.name.lower().rstrip("s")}
        for c in t.columns:
            terms |= set(c.name.lower().split("_"))
            terms |= {s.strip("'\"").lower() for s in c.samples}
        return len(q & terms)

    ranked = sorted(tables, key=score, reverse=True)
    chosen = {t.name for t in ranked[:max_tables] if score(t) > 0} or {ranked[0].name}
    by_name = {t.name: t for t in tables}
    for name in list(chosen):
        for _, ref, _ in by_name[name].foreign_keys:
            chosen.add(ref)
    return [t for t in tables if t.name in chosen]


def render_schema(tables: list[Table]) -> str:
    return "\n\n".join(t.ddl() for t in tables)

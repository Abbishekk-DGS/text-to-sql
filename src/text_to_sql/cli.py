"""text-to-sql: ask a SQLite database (or CSV files) questions in plain English.

text-to-sql demo                                    # builds demo/shop.db
text-to-sql ask "Which city has the most customers?" --db demo/shop.db
text-to-sql ask "average salary by team" --csv people.csv teams.csv
text-to-sql repl --db demo/shop.db
text-to-sql bench --model qwen2.5:3b                # execution accuracy on the demo benchmark
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .demo import BENCHMARK, build_demo, same_result
from .engine import OllamaLLM, Result, TextToSQL
from .schema import connect_readonly, load_csvs

DEMO_DB = Path("demo/shop.db")


def open_db(args) -> sqlite3.Connection:
    if getattr(args, "csv", None):
        return load_csvs([Path(p) for p in args.csv])
    db = Path(args.db)
    if not db.exists():
        if db == DEMO_DB:
            build_demo(db)
        else:
            sys.exit(f"database not found: {db}")
    return connect_readonly(db)


def table(result: Result, max_rows: int = 20) -> str:
    if not result.columns:
        return "(no columns)"
    rows = [[str(v) for v in r] for r in result.rows[:max_rows]]
    widths = [
        max(len(c), *(len(r[i]) for r in rows)) if rows else len(c)
        for i, c in enumerate(result.columns)
    ]
    line = " | ".join(c.ljust(w) for c, w in zip(result.columns, widths, strict=True))
    out = [line, "-+-".join("-" * w for w in widths)]
    out += [" | ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)) for r in rows]
    if len(result.rows) > max_rows:
        out.append(f"... {len(result.rows) - max_rows} more rows")
    return "\n".join(out)


def show(result: Result) -> None:
    for i, a in enumerate(result.attempts, 1):
        if a.error:
            print(f"  attempt {i} failed: {a.error}")
    if not result.ok:
        print(f"Could not answer: {result.error}")
        return
    print(f"\nSQL:\n  {result.sql}\n")
    print(table(result))
    if result.answer:
        print(f"\n{result.answer}")
    print(f"\n({len(result.rows)} rows, {result.seconds:.1f}s, {len(result.attempts)} attempt(s))")


def bench(sage: TextToSQL, gold_conn: sqlite3.Connection) -> float:
    correct = 0
    for q, gold_sql in BENCHMARK:
        res = sage.ask(q)
        gold = gold_conn.execute(gold_sql).fetchall()
        ok = res.ok and same_result(res.rows, gold)
        correct += ok
        retry = (
            f" (fixed after {len(res.attempts) - 1} retry)" if ok and len(res.attempts) > 1 else ""
        )
        print(f"{'PASS' if ok else 'FAIL'}  {q}{retry}")
        if not ok:
            print(f"      got: {res.sql or res.error}")
    acc = correct / len(BENCHMARK)
    print(f"\nExecution accuracy: {correct}/{len(BENCHMARK)} = {acc:.0%}")
    return acc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="text-to-sql",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__} · by Abbishekk DGS · abbishekkdgs.tech",
    )
    parser.add_argument("--model", default="qwen2.5:3b")
    parser.add_argument("--retries", type=int, default=2)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo")
    for name in ("ask", "repl", "bench"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=str(DEMO_DB))
        if name != "bench":
            p.add_argument("--csv", nargs="+")
            p.add_argument("--explain", action="store_true", help="also answer in plain English")
        if name == "ask":
            p.add_argument("question")
    args = parser.parse_args(argv)

    if args.command == "demo":
        print(f"Built {build_demo(DEMO_DB)}")
        return 0

    conn = open_db(args)
    sage = TextToSQL(conn, OllamaLLM(args.model), max_retries=args.retries)
    if args.command == "ask":
        show(sage.ask(args.question, explain=args.explain))
    elif args.command == "bench":
        bench(sage, conn)
    else:
        print(f"Tables: {', '.join(t.name for t in sage.tables)}. Ctrl-D to quit.")
        while True:
            try:
                q = input("\n? ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if q:
                show(sage.ask(q, explain=args.explain))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

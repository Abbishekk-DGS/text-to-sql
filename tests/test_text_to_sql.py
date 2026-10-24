import sqlite3

import pytest

from text_to_sql.demo import BENCHMARK, build_demo, same_result
from text_to_sql.engine import TextToSQL, extract_sql
from text_to_sql.guard import UnsafeSQL, check_and_limit
from text_to_sql.schema import connect_readonly, introspect, link_schema, load_csvs, render_schema


@pytest.fixture
def db(tmp_path):
    return build_demo(tmp_path / "shop.db")


class ScriptedLLM:
    """Replies with queued answers and records what it was shown."""

    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def chat(self, messages):
        self.seen.append(messages)
        return self.replies.pop(0)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE customers",
        "DELETE FROM orders",
        "SELECT 1; DROP TABLE customers",
        "UPDATE products SET price = 0",
        "INSERT INTO customers VALUES (1,'x','y','z')",
        "PRAGMA writable_schema = 1",
        "ATTACH DATABASE 'x.db' AS x",
        "WITH x AS (SELECT 1) DELETE FROM orders",
    ],
)
def test_guard_blocks_writes(sql):
    with pytest.raises(UnsafeSQL):
        check_and_limit(sql)


def test_guard_caps_limit():
    assert check_and_limit("SELECT * FROM t").endswith("LIMIT 200")
    assert check_and_limit("SELECT * FROM t LIMIT 5").endswith("LIMIT 5")
    assert check_and_limit("SELECT * FROM t LIMIT 99999", max_rows=50).endswith("LIMIT 50")
    assert "LIMIT" in check_and_limit("WITH a AS (SELECT 1 AS x) SELECT x FROM a")


def test_readonly_connection_rejects_writes(db):
    conn = connect_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM orders")


def test_extract_sql_variants():
    assert extract_sql("```sql\nSELECT 1;\n```") == "SELECT 1"
    assert extract_sql("Here you go:\nSELECT name FROM t;") == "SELECT name FROM t"


def test_schema_render_includes_fks_and_samples(db):
    tables = introspect(connect_readonly(db))
    rendered = render_schema(tables)
    assert "FOREIGN KEY (customer_id) REFERENCES customers(id)" in rendered
    assert "-- e.g." in rendered and "rows" in rendered
    # every status value is listed, so 'cancelled' can be matched exactly
    assert "-- one of" in rendered and "'cancelled'" in rendered


def test_schema_linking_keeps_fk_neighbours(db):
    tables = introspect(connect_readonly(db))
    linked = link_schema("total quantity in order items", tables, max_tables=1)
    assert {t.name for t in linked} == {"order_items", "orders", "products"}


def test_self_correction_loop(db):
    llm = ScriptedLLM(
        "```sql\nSELECT COUNT(*) FROM customer\n```",  # wrong table name
        "```sql\nSELECT COUNT(*) FROM customers\n```",
    )
    res = TextToSQL(connect_readonly(db), llm).ask("How many customers?")
    assert res.ok and res.rows == [(120,)]
    assert len(res.attempts) == 2 and "no such table" in res.attempts[0].error
    assert "no such table" in llm.seen[1][-1]["content"]  # the error was fed back


def test_unsafe_reply_is_retried_not_run(db):
    llm = ScriptedLLM("DELETE FROM orders", "SELECT COUNT(*) FROM orders")
    res = TextToSQL(connect_readonly(db), llm).ask("How many orders?")
    assert res.ok and res.rows == [(600,)]
    assert "safety guard" in res.attempts[0].error


def test_gold_benchmark_runs(db):
    conn = connect_readonly(db)
    for _, sql in BENCHMARK:
        assert conn.execute(sql).fetchall() is not None


def test_same_result_tolerates_extra_columns():
    assert same_result([("Chennai", 20)], [("Chennai",)])
    assert same_result([(1.004,)], [(1.0,)])
    assert not same_result([("Pune",)], [("Chennai",)])


def test_csv_loading(tmp_path):
    f = tmp_path / "Team Members.csv"
    f.write_text("Name,Age,Salary\nAsha,31,120000.5\nRavi,28,98000\n")
    conn = load_csvs([f])
    tables = introspect(conn)
    assert tables[0].name == "team_members"
    assert [c.type for c in tables[0].columns] == ["TEXT", "INTEGER", "REAL"]
    assert conn.execute("SELECT MAX(salary) FROM team_members").fetchone()[0] == 120000.5

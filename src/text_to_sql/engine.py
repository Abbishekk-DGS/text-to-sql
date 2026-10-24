"""Text-to-SQL loop: link schema -> generate -> guard -> execute -> self-correct on failure."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .guard import UnsafeSQL, check_and_limit
from .schema import introspect, link_schema, render_schema

_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_URL = _host if _host.startswith("http") else f"http://{_host}"

SYSTEM = """You are an expert SQLite analyst. Write ONE SQLite SELECT query that answers the question.
Rules:
- Use only the tables and columns in the schema. Quote identifiers that contain spaces.
- Join on the declared foreign keys.
- Use the sample values to match text exactly (case and spelling).
- For "top"/"most"/"highest" questions, ORDER BY and LIMIT.
- SQLite dates are TEXT 'YYYY-MM-DD': use strftime('%Y', col), date('now'), etc.
Reply with the SQL inside a ```sql code block and nothing else."""

USER = """Schema:
{schema}

Question: {question}"""

FIX = """That query failed.
SQL:
{sql}
Problem: {error}
Write a corrected query. Reply with the SQL inside a ```sql code block."""

ANSWER = """Question: {question}
SQL: {sql}
Result (first rows): {rows}
Answer the question in one or two plain sentences using the result."""


class LLM(Protocol):
    def chat(self, messages: list[dict]) -> str: ...


class OllamaLLM:
    def __init__(self, model: str = "qwen2.5:3b", url: str = OLLAMA_URL):
        self.model, self.url = model, url

    def chat(self, messages: list[dict]) -> str:
        resp = httpx.post(
            f"{self.url}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": 0, "num_ctx": 8192},
            },
            timeout=300,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]


def extract_sql(text: str) -> str:
    fenced = re.search(r"```(?:sql|sqlite)?\s*(.+?)```", text, re.S | re.I)
    sql = fenced.group(1) if fenced else text
    if not fenced:  # fall back to the first SELECT/WITH statement in the reply
        m = re.search(r"\b(with|select)\b.+", text, re.S | re.I)
        sql = m.group(0) if m else text
    return sql.strip().rstrip(";").strip()


@dataclass
class Attempt:
    sql: str
    error: str | None = None


@dataclass
class Result:
    question: str
    sql: str | None
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0
    answer: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class TextToSQL:
    def __init__(
        self, conn: sqlite3.Connection, llm: LLM, max_retries: int = 2, max_rows: int = 200
    ):
        self.conn, self.llm = conn, llm
        self.max_retries, self.max_rows = max_retries, max_rows
        self.tables = introspect(conn)

    def ask(self, question: str, explain: bool = False) -> Result:
        t0 = time.time()
        schema = render_schema(link_schema(question, self.tables))
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER.format(schema=schema, question=question)},
        ]
        result = Result(question=question, sql=None)
        for _ in range(self.max_retries + 1):
            reply = self.llm.chat(messages)
            sql = extract_sql(reply)
            attempt = Attempt(sql)
            result.attempts.append(attempt)
            try:
                safe = check_and_limit(sql, self.max_rows)
                cur = self.conn.execute(safe)
                result.sql = safe
                result.columns = [d[0] for d in cur.description or []]
                result.rows = cur.fetchall()
                result.error = None
                break
            except UnsafeSQL as e:
                attempt.error = f"rejected by safety guard: {e}"
            except sqlite3.Error as e:
                attempt.error = f"SQLite error: {e}"
            result.error = attempt.error
            messages += [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": FIX.format(sql=sql, error=attempt.error)},
            ]
        if result.ok and explain:
            result.answer = self.llm.chat(
                [
                    {
                        "role": "user",
                        "content": ANSWER.format(
                            question=question, sql=result.sql, rows=result.rows[:20]
                        ),
                    }
                ]
            ).strip()
        result.seconds = time.time() - t0
        return result

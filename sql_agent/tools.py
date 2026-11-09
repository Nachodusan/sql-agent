"""The four tools the agent can call.

Every tool returns a dict and never raises. A tool that fails returns
`{"ok": False, "error": ...}`, because a raised exception ends the run while a
returned error is something the agent can read and act on. The whole point of
this project is what happens on the step *after* a failure.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from sql_agent import db

MAX_ROWS = 50
FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|PRAGMA|VACUUM)\b",
    re.IGNORECASE,
)


class Tools:
    """Bound to one database path, so tests can point at a scratch database."""

    def __init__(self, db_path: Path | str | None = None, *, timeout_s: float = 5.0):
        self.db_path = Path(db_path) if db_path else db.DEFAULT_DB_PATH
        self.timeout_s = timeout_s

    # -- tool 1 ----------------------------------------------------------
    def list_tables(self) -> dict[str, Any]:
        """Every table, with its row count. The cheapest way to orient."""
        try:
            conn = db.connect(self.db_path)
            names = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            tables = [
                {"table": n, "rows": conn.execute(f"SELECT count(*) FROM {n}").fetchone()[0]}
                for n in names
            ]
            conn.close()
            return {"ok": True, "tables": tables}
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # -- tool 2 ----------------------------------------------------------
    def describe_table(self, table: str) -> dict[str, Any]:
        """Columns, types, foreign keys and three real rows.

        The sample rows matter more than the types: they are what reveals that
        money is stored in cents and that dates are ISO strings.
        """
        try:
            conn = db.connect(self.db_path)
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                known = [
                    r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    )
                ]
                conn.close()
                return {
                    "ok": False,
                    "error": f"no such table: {table}",
                    "hint": f"tables are: {', '.join(known)}",
                }

            columns = [
                {
                    "name": r["name"],
                    "type": r["type"],
                    "notnull": bool(r["notnull"]),
                    "pk": bool(r["pk"]),
                }
                for r in conn.execute(f"PRAGMA table_info({table})")
            ]
            foreign_keys = [
                {"column": r["from"], "references": f"{r['table']}.{r['to']}"}
                for r in conn.execute(f"PRAGMA foreign_key_list({table})")
            ]
            sample = [
                dict(r) for r in conn.execute(f"SELECT * FROM {table} LIMIT 3")
            ]
            conn.close()
            return {
                "ok": True,
                "table": table,
                "columns": columns,
                "foreign_keys": foreign_keys,
                "sample_rows": sample,
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # -- tool 3 ----------------------------------------------------------
    def run_sql(self, query: str) -> dict[str, Any]:
        """Run one read-only SELECT.

        Errors come back as text, deliberately. `no such column: total` is the
        single most useful thing this database can tell an agent, because it is
        the moment it has to stop guessing and go read the schema.
        """
        query = (query or "").strip().rstrip(";").strip()
        if not query:
            return {"ok": False, "error": "empty query"}
        if FORBIDDEN.search(query):
            return {
                "ok": False,
                "error": "only read-only SELECT queries are allowed",
            }
        if not re.match(r"^\s*(SELECT|WITH)\b", query, re.IGNORECASE):
            return {"ok": False, "error": "query must start with SELECT or WITH"}

        try:
            conn = db.connect(self.db_path)
            conn.execute(f"PRAGMA busy_timeout = {int(self.timeout_s * 1000)}")
            cur = conn.execute(query)
            rows = [dict(r) for r in cur.fetchmany(MAX_ROWS + 1)]
            conn.close()
        except sqlite3.Error as exc:
            # The agent reads this string. Keep SQLite's own wording.
            return {"ok": False, "error": str(exc), "query": query}
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "query": query}

        truncated = len(rows) > MAX_ROWS
        return {
            "ok": True,
            "query": query,
            "rows": rows[:MAX_ROWS],
            "row_count": len(rows[:MAX_ROWS]),
            "truncated": truncated,
        }

    # -- tool 4 ----------------------------------------------------------
    def finish(self, answer: str, sql: str = "") -> dict[str, Any]:
        """Terminal step. The agent states its answer and the SQL behind it."""
        return {"ok": True, "answer": answer, "sql": sql}

    # --------------------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        fn = {
            "list_tables": lambda a: self.list_tables(),
            "describe_table": lambda a: self.describe_table(a.get("table", "")),
            "run_sql": lambda a: self.run_sql(a.get("query", "")),
            "finish": lambda a: self.finish(a.get("answer", ""), a.get("sql", "")),
        }.get(name)
        if fn is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        return fn(arguments)


SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": "List every table in the database with its row count.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": (
                "Show a table's columns, types, foreign keys and three sample rows. "
                "Read this before writing SQL against a table you have not inspected."
            ),
            "parameters": {
                "type": "object",
                "properties": {"table": {"type": "string", "description": "Table name."}},
                "required": ["table"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Execute one read-only SELECT (or WITH) query and return up to 50 rows. "
                "If the query is invalid you get the database error back; read it and fix the query."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "A single SELECT statement."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Give the final answer. Call this only once you have run a query whose "
                "result actually answers the question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {"type": "string", "description": "The answer, in plain prose, with the figures."},
                    "sql": {"type": "string", "description": "The final SQL the answer rests on."},
                },
                "required": ["answer"],
            },
        },
    },
]

"""Per-step accounting.

An agent you cannot read is an agent you cannot debug, evaluate or put a budget
on. Every model call and every tool call lands here with its latency, its token
usage and what it cost, and the whole run serialises to JSON so the evaluation
harness can assert on the *path*, not just the final answer.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _usd_per_mtok(var: str, default: float) -> float:
    try:
        return float(os.getenv(var, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Step:
    n: int
    kind: str                      # "model" | "tool"
    name: str                      # model name, or tool name
    latency_ms: int
    ok: bool = True
    arguments: dict[str, Any] = field(default_factory=dict)
    summary: str = ""              # one line, for humans
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class Trace:
    question: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    steps: list[Step] = field(default_factory=list)
    answer: str | None = None
    final_sql: str | None = None          # SQL that was actually executed
    claimed_sql: str | None = None        # SQL the model typed into `finish`
    stopped_reason: str | None = None     # "finished" | "max_steps" | "error"

    # -- recording -------------------------------------------------------
    def add_model(self, model: str, latency_ms: int, usage: Any, summary: str) -> Step:
        pin = _usd_per_mtok("SQL_AGENT_COST_IN", 0.25)
        pout = _usd_per_mtok("SQL_AGENT_COST_OUT", 0.85)
        p = int(getattr(usage, "prompt_tokens", 0) or 0)
        c = int(getattr(usage, "completion_tokens", 0) or 0)
        step = Step(
            n=len(self.steps) + 1, kind="model", name=model, latency_ms=latency_ms,
            summary=summary, prompt_tokens=p, completion_tokens=c,
            cost_usd=round(p / 1e6 * pin + c / 1e6 * pout, 6),
        )
        self.steps.append(step)
        return step

    def add_tool(self, name: str, arguments: dict[str, Any], result: dict[str, Any],
                 latency_ms: int) -> Step:
        ok = bool(result.get("ok"))
        step = Step(
            n=len(self.steps) + 1, kind="tool", name=name, latency_ms=latency_ms,
            ok=ok, arguments=arguments, summary=_summarise(name, result),
            error=None if ok else str(result.get("error", "")),
        )
        self.steps.append(step)
        return step

    # -- totals ----------------------------------------------------------
    @property
    def cost_usd(self) -> float:
        return round(sum(s.cost_usd for s in self.steps), 6)

    @property
    def latency_ms(self) -> int:
        return sum(s.latency_ms for s in self.steps)

    @property
    def tool_calls(self) -> int:
        return sum(1 for s in self.steps if s.kind == "tool")

    @property
    def recoveries(self) -> int:
        """Failed tool calls that were followed by a successful one.

        This is the number the project exists to produce. A run with zero
        failures proves nothing about recovery; a run that fails and then
        stops proves the opposite of what we want.
        """
        tools = [s for s in self.steps if s.kind == "tool"]
        return sum(
            1 for i, s in enumerate(tools)
            if not s.ok and any(t.ok for t in tools[i + 1:])
        )

    @property
    def tool_path(self) -> list[str]:
        return [f"{s.name}{'' if s.ok else '!'}" for s in self.steps if s.kind == "tool"]

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "question": self.question,
            "answer": self.answer,
            "final_sql": self.final_sql,
            "claimed_sql": self.claimed_sql,
            "sql_was_executed": bool(self.final_sql),
            "stopped_reason": self.stopped_reason,
            "totals": {
                "steps": len(self.steps),
                "tool_calls": self.tool_calls,
                "recoveries": self.recoveries,
                "latency_ms": self.latency_ms,
                "cost_usd": self.cost_usd,
                "tool_path": self.tool_path,
            },
            "steps": [asdict(s) for s in self.steps],
        }

    def save(self, directory: Path | str = "traces") -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.run_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def render(self) -> str:
        """The plain-text trace that goes in the README."""
        lines = [f"question: {self.question}", ""]
        for s in self.steps:
            if s.kind == "model":
                lines.append(f"  [{s.n}] think            {s.latency_ms:>5} ms  "
                             f"{s.prompt_tokens}+{s.completion_tokens} tok  ${s.cost_usd:.4f}")
            else:
                mark = "ok " if s.ok else "ERR"
                detail = s.summary if s.ok else (s.error or "")
                lines.append(f"  [{s.n}] {s.name:<16} {s.latency_ms:>5} ms  {mark}  {detail}")
        lines += [
            "",
            f"  {len(self.steps)} steps · {self.tool_calls} tool calls · "
            f"{self.recoveries} recoveries · {self.latency_ms/1000:.1f}s · ${self.cost_usd:.4f}",
        ]
        if self.stopped_reason and self.stopped_reason != "finished":
            lines.append(f"  stopped: {self.stopped_reason}")
        return "\n".join(lines)


def _summarise(name: str, result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return str(result.get("error", ""))
    if name == "list_tables":
        return f"{len(result.get('tables', []))} tables"
    if name == "describe_table":
        cols = len(result.get("columns", []))
        return f"{result.get('table')}: {cols} columns"
    if name == "run_sql":
        n = result.get("row_count", 0)
        return f"{n} row{'s' if n != 1 else ''}" + (" (truncated)" if result.get("truncated") else "")
    if name == "finish":
        return "answered"
    return ""

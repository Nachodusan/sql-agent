"""The agent loop, as an explicit LangGraph state machine.

    START → think → (tool calls?) → act → think → … → END

`think` asks the model what to do next. `act` runs whatever it asked for and
writes the result back as an observation. The loop is bounded by
`SQL_AGENT_MAX_STEPS`, because the failure mode of an unbounded agent is not a
wrong answer, it is a bill.

Nothing here is clever. The interesting behaviour comes from `tools.run_sql`
handing the database's own error text back to the model instead of raising.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from sql_agent.llm import LLM
from sql_agent.tools import SCHEMAS, Tools
from sql_agent.trace import Trace

SYSTEM = """You answer questions about a SQLite database by querying it.

Work like this:
1. Look at what tables exist.
2. Read the schema of the tables you intend to use, including the sample rows.
3. Write one SELECT that answers the question, and run it.
4. Call finish with the answer and the SQL behind it.

Rules that matter in this database:
- Never assume a column exists. Check with describe_table first.
- If a query errors, read the error, fix the query, and try again. Errors are
  information, not a reason to give up or to invent a result.
- Money is stored as INTEGER cents. Divide by 100.0 to report currency.
- Never report a number you have not seen in a query result.
- If the question is ambiguous about what to include, state the choice you made
  in your answer.

Answer in the language the question was asked in."""


class State(TypedDict):
    messages: list[dict[str, Any]]
    trace: Trace
    done: bool


def _tool_result_message(call_id: str, name: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": json.dumps(result, ensure_ascii=False, default=str),
    }


def build_graph(llm: LLM, tools: Tools, max_steps: int):
    def think(state: State) -> dict[str, Any]:
        trace = state["trace"]
        reply = llm.chat(state["messages"], SCHEMAS)
        msg = reply.message
        calls = getattr(msg, "tool_calls", None) or []
        summary = (
            "→ " + ", ".join(c.function.name for c in calls) if calls
            else (msg.content or "")[:70]
        )
        trace.add_model(llm.model, reply.latency_ms, reply.usage, summary)

        # Normalise the assistant turn back into plain dicts, so the message
        # list stays JSON-serialisable and provider-agnostic.
        assistant: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if calls:
            assistant["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.function.name, "arguments": c.function.arguments},
                }
                for c in calls
            ]
        return {"messages": state["messages"] + [assistant]}

    def act(state: State) -> dict[str, Any]:
        trace = state["trace"]
        messages = list(state["messages"])
        calls = messages[-1].get("tool_calls", [])
        done = False

        for call in calls:
            name = call["function"]["name"]
            raw = call["function"].get("arguments") or "{}"
            try:
                arguments = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError as exc:
                result = {"ok": False, "error": f"arguments were not valid JSON: {exc}"}
                trace.add_tool(name, {"raw": str(raw)[:200]}, result, 0)
                messages.append(_tool_result_message(call["id"], name, result))
                continue

            # Smaller models sometimes send a JSON array, or a bare string,
            # where the schema asks for an object. That is a malformed call,
            # not a crash: tell the model what shape it owes us and let it
            # try again.
            if not isinstance(arguments, dict):
                result = {
                    "ok": False,
                    "error": (
                        f"arguments must be a JSON object, got "
                        f"{type(arguments).__name__}. Expected keys for {name}."
                    ),
                }
                trace.add_tool(name, {"raw": str(raw)[:200]}, result, 0)
                messages.append(_tool_result_message(call["id"], name, result))
                continue

            t0 = time.perf_counter()
            result = tools.call(name, arguments)
            trace.add_tool(name, arguments, result, int((time.perf_counter() - t0) * 1000))
            messages.append(_tool_result_message(call["id"], name, result))

            if name == "finish" and result.get("ok"):
                trace.answer = result.get("answer")
                # Prefer SQL we actually executed over SQL the model typed into
                # `finish`. Asked how many orders there were, the agent read the
                # count off `list_tables` and then reported a SELECT it had
                # never run — a right answer with fabricated evidence.
                trace.claimed_sql = result.get("sql") or None
                trace.final_sql = _last_successful_sql(trace)
                trace.stopped_reason = "finished"
                done = True

        return {"messages": messages, "done": done}

    def route_after_think(state: State) -> str:
        last = state["messages"][-1]
        if last.get("tool_calls"):
            return "act"
        # The model answered in prose without calling finish. Accept it rather
        # than burning another turn, but record that it skipped the tool.
        trace = state["trace"]
        trace.answer = last.get("content") or ""
        trace.final_sql = _last_successful_sql(trace)
        trace.stopped_reason = "answered_without_finish"
        return END

    def route_after_act(state: State) -> str:
        if state["done"]:
            return END
        if len(state["trace"].steps) >= max_steps:
            state["trace"].stopped_reason = "max_steps"
            return END
        return "think"

    graph = StateGraph(State)
    graph.add_node("think", think)
    graph.add_node("act", act)
    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", route_after_think, {"act": "act", END: END})
    graph.add_conditional_edges("act", route_after_act, {"think": "think", END: END})
    return graph.compile()


def _last_successful_sql(trace: Trace) -> str | None:
    for step in reversed(trace.steps):
        if step.kind == "tool" and step.name == "run_sql" and step.ok:
            return step.arguments.get("query")
    return None


def ask(
    question: str,
    *,
    db_path: Path | str | None = None,
    model: str | None = None,
    max_steps: int | None = None,
    llm: LLM | None = None,
) -> Trace:
    """Answer one question. Returns the trace; the answer is `trace.answer`."""
    max_steps = max_steps or int(os.getenv("SQL_AGENT_MAX_STEPS", "12"))
    timeout_s = float(os.getenv("SQL_AGENT_SQL_TIMEOUT_S", "5"))
    tools = Tools(db_path, timeout_s=timeout_s)
    llm = llm or LLM(model)

    trace = Trace(question=question)
    app = build_graph(llm, tools, max_steps)
    app.invoke(
        {
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": question},
            ],
            "trace": trace,
            "done": False,
        },
        {"recursion_limit": max_steps * 2 + 4},
    )
    if trace.stopped_reason is None:
        trace.stopped_reason = "max_steps"
    return trace

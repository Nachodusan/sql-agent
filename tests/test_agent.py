"""Tests for the loop itself, with a scripted model.

What is asserted here is the *path*: that a failed tool call is followed by a
recovery, that the budget is enforced, and that a malformed tool call does not
take the run down.
"""

from __future__ import annotations

from sql_agent.agent import ask
from tests.conftest import ScriptedLLM

GOOD_SQL = (
    "SELECT c.name, SUM(oi.quantity * oi.unit_price_cents) / 100.0 AS spend "
    "FROM customers c "
    "JOIN orders o ON o.customer_id = c.id AND o.status != 'cancelled' "
    "JOIN order_items oi ON oi.order_id = o.id "
    "GROUP BY c.id ORDER BY spend DESC LIMIT 3"
)


def test_recovers_from_a_bad_column_and_answers(shop_db):
    """The headline behaviour: guess, fail, read the schema, succeed."""
    llm = ScriptedLLM([
        [("list_tables", {})],
        [("run_sql", {"query": "SELECT customer_id, total FROM orders"})],   # fails
        [("describe_table", {"table": "order_items"})],
        [("run_sql", {"query": GOOD_SQL})],
        [("finish", {"answer": "Top 3 by spend.", "sql": GOOD_SQL})],
    ])
    trace = ask("top 3 customers by spend", db_path=shop_db, llm=llm)

    assert trace.stopped_reason == "finished"
    assert trace.answer == "Top 3 by spend."
    assert trace.tool_path == ["list_tables", "run_sql!", "describe_table", "run_sql", "finish"]
    assert trace.recoveries == 1
    assert trace.final_sql == GOOD_SQL


def test_a_clean_run_records_no_recovery(shop_db):
    llm = ScriptedLLM([
        [("run_sql", {"query": "SELECT count(*) AS n FROM orders"})],
        [("finish", {"answer": "281 orders.", "sql": "SELECT count(*) FROM orders"})],
    ])
    trace = ask("how many orders are there", db_path=shop_db, llm=llm)
    assert trace.recoveries == 0
    assert trace.stopped_reason == "finished"


def test_max_steps_stops_a_looping_agent(shop_db):
    """An agent that never calls finish must stop on budget, not on patience."""
    llm = ScriptedLLM([[("list_tables", {})] for _ in range(50)])
    trace = ask("anything", db_path=shop_db, llm=llm, max_steps=6)
    assert trace.stopped_reason == "max_steps"
    assert len(trace.steps) <= 8          # the cap is checked after each act
    assert trace.answer is None


def test_malformed_tool_arguments_do_not_kill_the_run(shop_db):
    llm = ScriptedLLM([
        "placeholder",                     # replaced below
        [("finish", {"answer": "recovered", "sql": ""})],
    ])
    # Hand-craft a turn whose arguments are not valid JSON.
    from tests.conftest import FakeFunction, FakeMessage, FakeReply, FakeToolCall, FakeUsage

    broken = FakeReply(
        FakeMessage(tool_calls=[FakeToolCall("call_x", FakeFunction("run_sql", "{not json"))]),
        FakeUsage(),
    )
    original = llm.chat
    state = {"first": True}

    def chat(messages, tools):
        if state["first"]:
            state["first"] = False
            llm.turns.pop(0)
            llm.calls.append(list(messages))
            return broken
        return original(messages, tools)

    llm.chat = chat
    trace = ask("anything", db_path=shop_db, llm=llm)
    assert trace.stopped_reason == "finished"
    bad = [s for s in trace.steps if s.kind == "tool" and not s.ok]
    assert bad and "valid JSON" in bad[0].error


def test_prose_answer_without_finish_is_accepted_but_flagged(shop_db):
    llm = ScriptedLLM([
        [("run_sql", {"query": "SELECT count(*) AS n FROM orders"})],
        "There are 281 orders.",
    ])
    trace = ask("how many orders", db_path=shop_db, llm=llm)
    assert trace.stopped_reason == "answered_without_finish"
    assert "281" in trace.answer


def test_trace_accounting(shop_db):
    llm = ScriptedLLM([
        [("run_sql", {"query": "SELECT 1 AS one"})],
        [("finish", {"answer": "one", "sql": "SELECT 1"})],
    ])
    trace = ask("trivial", db_path=shop_db, llm=llm)

    assert trace.tool_calls == 2
    assert trace.cost_usd > 0
    assert trace.latency_ms >= 0

    payload = trace.to_dict()
    assert payload["totals"]["tool_path"] == ["run_sql", "finish"]
    assert len(payload["steps"]) == len(trace.steps)

    rendered = trace.render()
    assert "run_sql" in rendered and "steps" in rendered


def test_array_arguments_are_rejected_without_crashing(shop_db):
    """Found by agent-evals running a weaker model against this agent.

    Llama-3.1-8B sometimes sends `arguments` as a JSON array where the schema
    asks for an object. The old code called .get() on the parsed list and the
    whole run died with an AttributeError.
    """
    llm = ScriptedLLM([
        "placeholder",
        [("finish", {"answer": "recovered", "sql": ""})],
    ])
    from tests.conftest import FakeFunction, FakeMessage, FakeReply, FakeToolCall, FakeUsage

    malformed = FakeReply(
        FakeMessage(tool_calls=[FakeToolCall("c1", FakeFunction("run_sql", '["SELECT 1"]'))]),
        FakeUsage(),
    )
    original, state = llm.chat, {"first": True}

    def chat(messages, tools):
        if state["first"]:
            state["first"] = False
            llm.turns.pop(0)
            return malformed
        return original(messages, tools)

    llm.chat = chat
    trace = ask("anything", db_path=shop_db, llm=llm)

    assert trace.stopped_reason == "finished"
    bad = [s for s in trace.steps if s.kind == "tool" and not s.ok]
    assert bad and "must be a JSON object" in bad[0].error


def test_final_sql_only_reports_sql_that_ran(shop_db):
    """The agent may answer from list_tables and then type a SELECT into
    finish that it never executed. That claim is recorded separately."""
    llm = ScriptedLLM([
        [("list_tables", {})],
        [("finish", {"answer": "245 orders.", "sql": "SELECT count(*) FROM orders"})],
    ])
    trace = ask("how many orders", db_path=shop_db, llm=llm)

    assert trace.final_sql is None                       # nothing was executed
    assert trace.claimed_sql == "SELECT count(*) FROM orders"
    assert trace.to_dict()["sql_was_executed"] is False


def test_final_sql_prefers_what_was_executed(shop_db):
    llm = ScriptedLLM([
        [("run_sql", {"query": "SELECT count(*) AS n FROM orders"})],
        [("finish", {"answer": "245.", "sql": "SELECT something_else"})],
    ])
    trace = ask("how many orders", db_path=shop_db, llm=llm)
    assert trace.final_sql == "SELECT count(*) AS n FROM orders"
    assert trace.claimed_sql == "SELECT something_else"
    assert trace.to_dict()["sql_was_executed"] is True

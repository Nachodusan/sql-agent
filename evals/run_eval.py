"""Run the golden set and report.

Scoring compares *result sets*, not text. The agent's final SQL and the
reference SQL both run against the same database; the case passes when they
return the same rows. Two correct queries almost never look alike, so grading
the SQL string would mostly measure style.

Alongside correctness it reports the things that decide whether an agent is
deployable: how many steps it took, how often it had to recover, how long it
took and what it cost.

    python evals/run_eval.py              # the whole set
    python evals/run_eval.py --only top_customers_net
    python evals/run_eval.py --json report.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sql_agent import db                     # noqa: E402
from sql_agent.agent import ask              # noqa: E402

GOLDEN = Path(__file__).with_name("golden.yaml")


def normalise(rows: list[sqlite3.Row]) -> list[tuple]:
    """Compare by value, tolerating column aliases and float noise."""
    out = []
    for row in rows:
        values = []
        for v in tuple(row):
            if isinstance(v, float):
                values.append(round(v, 2))
            else:
                values.append(v)
        out.append(tuple(values))
    return out


def _contains_in_order(haystack: tuple, needle: tuple) -> bool:
    """Is `needle` a subsequence of `haystack`?

    The agent is free to select more columns than the reference does. Asking
    for `name, spend` and getting back `id, name, gross, refunds, spend` is a
    cosmetic difference, not a wrong answer.
    """
    it = iter(haystack)
    return all(any(value == h for h in it) for value in needle)


def matches(expected: list[tuple], actual: list[tuple], *, ordered: bool) -> bool:
    """Does the agent's result carry the reference's answer?

    Two rules that cost a round of false failures to learn:

    * **Row order is only compared when the question asked for an order.**
      "Revenue per category" has no canonical order, so grading on it measures
      nothing. "Top 5 customers" does, so there it is part of the answer.
    * **Extra columns are allowed.** Only the reference's values have to be
      present, in order, within each row.
    """
    if len(expected) != len(actual):
        return False
    if ordered:
        return all(_contains_in_order(a, e) for e, a in zip(expected, actual))
    remaining = list(actual)
    for row in expected:
        for i, candidate in enumerate(remaining):
            if _contains_in_order(candidate, row):
                remaining.pop(i)
                break
        else:
            return False
    return True


def run_query(db_path: Path, query: str) -> tuple[list[tuple] | None, str | None]:
    try:
        conn = db.connect(db_path)
        rows = normalise(conn.execute(query).fetchall())
        conn.close()
        return rows, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", dest="db_path", default=None)
    parser.add_argument("--only", default=None, help="Run a single case by id.")
    parser.add_argument("--json", dest="json_out", default=None, help="Write the report here.")
    args = parser.parse_args(argv)

    db_path = Path(args.db_path) if args.db_path else db.DEFAULT_DB_PATH
    if not db_path.exists():
        db.build(db_path)

    cases = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
        if not cases:
            print(f"no case with id {args.only!r}", file=sys.stderr)
            return 2

    results = []
    for case in cases:
        expected, ref_err = run_query(db_path, case["reference_sql"])
        if ref_err:
            print(f"✗ {case['id']}: the reference query itself is broken — {ref_err}")
            results.append({"id": case["id"], "passed": False, "reason": "bad_reference"})
            continue

        trace = ask(case["question"], db_path=db_path)
        actual, agent_err = (None, "agent produced no SQL")
        if trace.final_sql:
            actual, agent_err = run_query(db_path, trace.final_sql)

        ordered = bool(case.get("ordered", False))
        passed = actual is not None and matches(expected, actual, ordered=ordered)
        reason = None if passed else (agent_err or "result mismatch")

        mark = "✓" if passed else "✗"
        print(
            f"{mark} {case['id']:<24} "
            f"{len(trace.steps):>2} steps  {trace.recoveries} rec  "
            f"{trace.latency_ms/1000:>5.1f}s  ${trace.cost_usd:.4f}"
            + ("" if passed else f"   → {reason}")
        )

        results.append({
            "id": case["id"],
            "question": case["question"],
            "traps": case.get("traps", []),
            "passed": passed,
            "reason": reason,
            "steps": len(trace.steps),
            "tool_path": trace.tool_path,
            "recoveries": trace.recoveries,
            "latency_ms": trace.latency_ms,
            "cost_usd": trace.cost_usd,
            "stopped_reason": trace.stopped_reason,
            "agent_sql": trace.final_sql,
            "expected_rows": len(expected),
        })

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    summary = {
        "passed": passed,
        "total": total,
        "accuracy": round(passed / total, 3) if total else 0.0,
        "recovery_rate": round(
            sum(1 for r in results if r.get("recoveries", 0) > 0) / total, 3
        ) if total else 0.0,
        "avg_steps": round(sum(r.get("steps", 0) for r in results) / total, 1) if total else 0,
        "total_cost_usd": round(sum(r.get("cost_usd", 0) for r in results), 4),
    }

    print()
    print(f"  {passed}/{total} correct · "
          f"{summary['recovery_rate']*100:.0f}% of runs needed a recovery · "
          f"{summary['avg_steps']} steps avg · ${summary['total_cost_usd']:.4f} total")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"summary": summary, "cases": results}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  report → {args.json_out}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

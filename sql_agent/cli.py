"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from sql_agent import db


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="sql-agent",
        description="Ask a question in plain language; the agent queries the database and shows its work.",
    )
    parser.add_argument("question", nargs="*", help="The question to answer.")
    parser.add_argument("--db", dest="db_path", default=None, help="Path to the SQLite file.")
    parser.add_argument("--model", default=None, help="Override SQL_AGENT_MODEL.")
    parser.add_argument("--max-steps", type=int, default=None, help="Hard cap on steps.")
    parser.add_argument("--json", action="store_true", help="Print the trace as JSON.")
    parser.add_argument("--save", action="store_true", help="Write the trace to traces/.")
    parser.add_argument("--build-db", action="store_true", help="Rebuild the sample database and exit.")
    args = parser.parse_args(argv)

    if args.build_db:
        path = db.build(args.db_path or db.DEFAULT_DB_PATH)
        print(f"built {path}")
        return 0

    question = " ".join(args.question).strip()
    if not question:
        parser.error("give me a question, or use --build-db")

    from sql_agent.agent import ask  # imported late so --build-db needs no API key

    try:
        trace = ask(question, db_path=args.db_path, model=args.model, max_steps=args.max_steps)
    except RuntimeError as exc:
        # Missing configuration is a user error, not a crash. No traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        import json
        print(json.dumps(trace.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(trace.render())
        print()
        print(trace.answer or "(no answer)")
        if trace.final_sql:
            print()
            print(trace.final_sql)

    if args.save:
        print(f"\ntrace → {trace.save()}")

    return 0 if trace.stopped_reason in ("finished", "answered_without_finish") else 1


if __name__ == "__main__":
    sys.exit(main())

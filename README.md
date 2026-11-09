# sql-agent

A natural-language SQL agent that **recovers from its own mistakes**, and shows
you every step it took, what it cost and how long it spent.

Ask a question in plain language. The agent inspects the schema, writes SQL,
runs it, and when the query fails it reads the database's error and fixes it.
Every model call and tool call is recorded with latency, tokens and cost, and
the whole run serialises to JSON.

```
$ sql-agent "Who are the top 5 customers by how much they actually spent?"
```

---

## Why the failures are the point

Most agent demos show a clean path: one question, one query, one answer. That
path proves the model can write SQL. It does not prove you have an agent.

The interesting behaviour is what happens on the step *after* something breaks.
So the sample database is built to break naive queries:

| Trap | What a guessing agent does | What it has to do instead |
|---|---|---|
| `orders` has **no total column** | `SELECT total FROM orders` → `no such column: total` | join `order_items`, sum `quantity * unit_price_cents` |
| Money is **INTEGER cents** | reports `78900` as the dollar figure | divide by `100.0` |
| `unit_price_cents` ≠ `products.list_price_cents` | joins to the catalogue price | use the price actually paid |
| `status` includes `cancelled` | counts cancelled orders as revenue | filter them out |
| `refunds` exists in its own table | reports gross as if it were net | subtract refunds |
| `support_tickets` is a plausible distractor | joins it for no reason | ignore it |

None of this is staged. `tools.run_sql` returns SQLite's own error string
verbatim, the model reads it, and the next step is a real decision rather than a
replayed script. There is a test asserting `orders` never grows a `total`
column, because the day it does, the demo becomes theatre.

## A real run

<!-- TRACE -->

## How it works

```
START → think → (tool calls?) → act → think → … → END
```

A LangGraph state machine with two nodes. `think` asks the model what to do
next; `act` runs whatever it asked for and writes the result back as an
observation. Four tools:

| Tool | Returns |
|---|---|
| `list_tables` | every table with its row count |
| `describe_table` | columns, types, foreign keys, **and three real rows** |
| `run_sql` | up to 50 rows, or the database error as text |
| `finish` | the final answer plus the SQL behind it |

Three decisions worth naming:

**Tools never raise.** A failing tool returns `{"ok": false, "error": ...}`. A
raised exception ends the run; a returned error is something the agent can act
on. That single choice is what makes recovery possible at all.

**`describe_table` returns sample rows, not just types.** `INTEGER` does not
tell you a column holds cents. `18900` does.

**The loop is bounded.** `SQL_AGENT_MAX_STEPS` caps it, and the cap is enforced
in the graph rather than trusted to the prompt. The failure mode of an unbounded
agent is not a wrong answer, it is a bill.

The database is opened **read-only at the connection level**, and `run_sql`
additionally rejects anything that is not a `SELECT` or `WITH`. Two independent
guards, because the prompt is not a security boundary.

## Evaluation

```
python evals/run_eval.py --json report.json
```

Eight cases in [`evals/golden.yaml`](evals/golden.yaml), each with a reference
query written by hand against the real schema, and each labelled with the traps
it is testing.

**Scoring compares result sets, not text.** Not the agent's prose, and not its
SQL either — two correct queries rarely look alike, so grading the SQL string
would mostly measure style. The agent's final query and the reference query both
run against the same database, and the case passes when they return the same
rows.

Alongside correctness, the report carries the numbers that decide whether an
agent is deployable:

| Metric | Why it is there |
|---|---|
| accuracy | did it get the right answer |
| recovery rate | how often it had to climb out of a hole |
| avg steps | how much it flails |
| cost per question | what a thousand of these would cost |
| tool path | *which* tools, in what order — the trajectory, not just the outcome |

Trajectory is the part most eval setups skip. An agent that reaches the right
number after nine confused steps and an agent that reaches it in three are not
the same agent, and only one of them is affordable.

## Running it

Requires Python ≥ 3.11 and any OpenAI-compatible endpoint.

```bash
git clone https://github.com/Nachodusan/sql-agent
cd sql-agent
uv venv && uv pip install ".[dev]"        # or: pip install ".[dev]"

cp .env.example .env                        # add your key
python -m sql_agent.cli --build-db          # generate the sample database

python -m sql_agent.cli "how many orders came through each channel in Q1?"
python -m sql_agent.cli "top 5 customers by spend" --save    # writes traces/<id>.json
```

The provider is configured by environment, not by code:

```bash
OPENAI_BASE_URL=https://api.deepinfra.com/v1/openai
SQL_AGENT_MODEL=deepseek-ai/DeepSeek-V3
```

Point it at OpenAI, Together, Groq or a local server and nothing else changes.

### Tests

```bash
pytest
```

Sixteen tests, all offline. The agent tests drive the loop with a **scripted
model** that replays a fixed list of turns, so the suite is deterministic and
costs nothing. Testing against a live model tests the model; these test the
loop — that a failed call is followed by a recovery, that the step budget is
enforced, that malformed tool arguments do not take the run down.

## What this is not

- **Not a text-to-SQL benchmark entry.** It is a working loop with honest
  instrumentation, on a database designed to be awkward.
- **Not safe for a production database as-is.** Read-only connection plus a
  statement filter is enough for a sample database, not for one holding real
  customer data.
- **Not model-agnostic in quality.** The loop works with any tool-calling model;
  how often it recovers on the first try very much depends on which one.

## Licence

[MIT](LICENSE).

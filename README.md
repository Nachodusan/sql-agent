# sql-agent

[![tests](https://github.com/Nachodusan/sql-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/Nachodusan/sql-agent/actions/workflows/tests.yml)

A natural-language SQL agent with an instrumented loop, and an evaluation
harness built to catch the answers that **look** right.

Ask a question in plain language. The agent inspects the schema, writes SQL,
runs it and answers. Every model call and tool call is recorded with latency,
tokens and cost; the whole run serialises to JSON; and a golden set grades the
agent on whether its query returns the same rows as a hand-written reference.

```
$ python -m sql_agent.cli "Who are the top 5 customers by how much they actually spent?"
```

**Measured result: 6/8 on the golden set with DeepSeek-V3, $0.0136 for the
whole run.** Both failures are answers that read perfectly and are wrong.

---

## What this project actually found

I built it expecting to demonstrate error recovery: the database is seeded with
traps, `run_sql` hands SQLite's error text back to the model, and there is a
test proving a failed call is followed by a successful one.

**The recovery path never fired.** Across eight questions and two models, the
measured recovery rate is **0%**. Both DeepSeek-V3 and Llama-3.1-8B call
`describe_table` before writing SQL, so neither ever hits `no such column:
total`. The machinery works — the unit tests exercise it — but on this schema,
with these models, it is not what goes wrong.

What goes wrong is worse. The agent writes **valid SQL that returns a plausible
wrong answer**, and then describes it fluently.

### The failure that makes the case

Asked for the top 5 customers by net spend, DeepSeek-V3 produced this:

```sql
JOIN order_items oi ON o.id = oi.order_id
LEFT JOIN refunds r ON o.id = r.order_id
```

Hanging both `order_items` and `refunds` off `orders` fans out the join: each
refund is counted once per line item in its order. `SUM(r.amount_cents)`
over-counts, and net spend is deflated for exactly the customers who bought the
most items.

|  | reference | agent |
|---|---|---|
| 1 | Joaquín Figueroa — 11,104.00 | Joaquín Figueroa — 11,104.00 |
| 2 | Isabella Lagos — 10,584.00 | Isabella Lagos — 10,584.00 |
| 3 | **Paz Cabrera — 8,552.50** | Thiago Rojas — 8,012.00 |
| 4 | **Dante Figueroa — 8,082.00** | Sofía Muñoz — 7,094.00 |
| 5 | Thiago Rojas — 8,012.00 | **Paz Cabrera — 6,841.00** |

The top two are right. The prose was confident. No error, no NULL, no
suspicious zero — just a number that is wrong by $1,711.50 and a customer who
silently drops two places.

Nothing short of running the query and comparing rows catches this.

Llama-3.1-8B failed the same question differently: it filtered
`status = 'completed'` (dropping shipped orders, which nobody asked to exclude),
returned customer IDs instead of names, and used `SUM(r.amount_cents)` without
`COALESCE`, so every customer with no refunds evaluated to NULL and fell out of
the ranking entirely. Same loop, same prompt, a different wrong answer.

## A real run

```
question: Who are the top 5 customers by how much they actually spent? …

  [1] think             3747 ms  856+18 tok   $0.0002
  [2] list_tables          6 ms  ok   6 tables
  [3] think             5203 ms  1005+88 tok  $0.0003
  [4] describe_table       0 ms  ok   customers: 5 columns
  [5] describe_table       0 ms  ok   orders: 5 columns
  [6] describe_table       0 ms  ok   order_items: 5 columns
  [7] describe_table       0 ms  ok   refunds: 5 columns
  [8] think            45766 ms  2428+523 tok $0.0011
  [9] run_sql              4 ms  ok   5 rows
  [10] think            30060 ms  3105+329 tok $0.0011
  [11] finish               0 ms  ok   answered

  11 steps · 7 tool calls · 0 recoveries · 84.8s · $0.0027
```

That is the run from the table above — the one with the wrong answer. Eleven
steps, no errors, under three tenths of a cent, and incorrect. This is why the
trace alone is not enough and the grader has to execute.

## The database

Shaped like a real operational schema rather than a tutorial one, so that
guessing is punished:

| Trap | What a guessing agent does | What it has to do |
|---|---|---|
| `orders` has **no total column** | `SELECT total FROM orders` → `no such column` | join `order_items`, sum `quantity * unit_price_cents` |
| Money is **INTEGER cents** | reports `78900` as a dollar figure | divide by `100.0` |
| `unit_price_cents` ≠ `products.list_price_cents` | joins to today's catalogue price | use the price actually paid |
| `status` includes `cancelled` | counts cancelled orders as revenue | filter them out |
| `refunds` is its own table | reports gross as net | subtract, **without fanning out the join** |
| `support_tickets` | joins it for no reason | ignore it |

A test asserts `orders` never grows a `total` column, because the day it does,
the trap becomes theatre. Seeding is deterministic, so the golden answers hold
across machines.

## How it works

```
START → think → (tool calls?) → act → think → … → END
```

A LangGraph state machine with two nodes. `think` asks the model what to do
next; `act` runs it and writes the result back as an observation.

| Tool | Returns |
|---|---|
| `list_tables` | every table with its row count |
| `describe_table` | columns, types, foreign keys, **and three real rows** |
| `run_sql` | up to 50 rows, or the database error as text |
| `finish` | the final answer plus the SQL behind it |

**Tools never raise.** A failing tool returns `{"ok": false, "error": ...}`. An
exception ends the run; a returned error is something the agent can act on.

**`describe_table` returns sample rows, not just types.** `INTEGER` does not
tell you a column holds cents. `18900` does.

**The loop is bounded** by `SQL_AGENT_MAX_STEPS`, enforced in the graph rather
than requested in the prompt. The failure mode of an unbounded agent is not a
wrong answer, it is a bill.

The database is opened **read-only at the connection level** and `run_sql`
separately rejects anything that is not `SELECT`/`WITH`. Two independent
guards, because a prompt is not a security boundary.

## Evaluation

```
python evals/run_eval.py --json report.json
```

Eight cases in [`evals/golden.yaml`](evals/golden.yaml), each with a reference
query written by hand and labelled with the traps it tests.

**Grading compares result sets, not text.** Not the prose, and not the SQL
string either — two correct queries rarely look alike, so grading the text
mostly measures style. The agent's final query and the reference both run
against the same database.

Two rules that cost a round of false failures to learn:

- **Row order is compared only when the question asked for one.** Cases carry
  `ordered: true` when the ranking is the answer.
- **Extra columns are allowed.** Asking for `name, spend` and getting back
  `id, name, gross, refunds, spend` is cosmetic.

The first version of this harness had neither rule and reported **3/8**. Two of
those failures were correct answers returned in a different row order, on
questions that never specified one, and a third was an ambiguous question —
*"what share"* got `0.1553` against a reference of `15.53`, and both readings
are defensible. The real number was 6/8. A grader that can be wrong about the
agent needs [its own tests](tests/test_eval_matching.py), so it has eight.

Reported per run, alongside accuracy: recovery rate, average steps, cost per
question, and the **tool path** — which tools in what order. An agent that
reaches the right number in nine confused steps and one that does it in three
are not the same agent, and only one of them is affordable.

### Current numbers

DeepSeek-V3 via DeepInfra, full golden set:

| | |
|---|---|
| accuracy | **6/8** |
| recovery rate | **0%** |
| avg steps | 8.8 |
| total cost | $0.0136 |
| failures | 1 join fan-out, 1 missing `GROUP BY` |

The `price_drift` failure is the other silent one: the agent omitted the
`GROUP BY` and returned the same product once per order line until it hit the
50-row cap.

## Evaluated from the outside

[agent-evals](https://github.com/Nachodusan/agent-evals) runs this agent's own
golden set three times per case and checks the trajectory, not just the answer.
It found two bugs this harness could not:

- **The agent reported SQL it never executed.** Asked how many orders there
  were, it read the count off `list_tables` and typed a `SELECT COUNT(*)` into
  its final answer. The outcome grader scored it correct, on fabricated
  evidence. `final_sql` now only ever holds SQL that actually ran; what the
  model claimed is kept separately as `claimed_sql`.
- **Malformed tool arguments crashed the run.** Llama-3.1-8B sends `arguments`
  as a JSON array where the schema asks for an object; the parsed list reached
  `.get()` and the whole attempt died. Six of its twenty-four attempts were
  crashes scored as model failures. They now come back as a tool error the
  model can correct.

That evaluation also disagrees with the number below: three runs per case put
this agent at **4/8 strict**, not 6/8. One of the passing cases was a coin
flip.

## Running it

Python ≥ 3.11 and any OpenAI-compatible endpoint.

```bash
git clone https://github.com/Nachodusan/sql-agent
cd sql-agent
uv venv && uv pip install ".[dev]"        # or: pip install ".[dev]"

cp .env.example .env                      # add your key
python -m sql_agent.cli --build-db        # generate the sample database

python -m sql_agent.cli "how many orders came through each channel in Q1?"
python -m sql_agent.cli "top 5 customers by spend" --save   # writes traces/<id>.json
```

The provider is configured by environment, not code:

```bash
OPENAI_BASE_URL=https://api.deepinfra.com/v1/openai
SQL_AGENT_MODEL=deepseek-ai/DeepSeek-V3
```

Point it at OpenAI, Together, Groq or a local server; nothing else changes.

> Editable installs (`pip install -e`) rely on `.pth` files, which Python 3.15
> no longer processes. The flat layout does not need one — running from the
> repository root works.

### Tests

```bash
pytest
```

Twenty-four tests, all offline. The agent tests drive the loop with a
**scripted model** replaying fixed turns, so the suite is deterministic and
free. Testing against a live model tests the model; these test the loop — that
a failed call is followed by a recovery, that the step budget stops a looping
agent, that malformed tool arguments do not take the run down.

## What this is not

- **Not a text-to-SQL benchmark entry.** It is a working loop with honest
  instrumentation on a deliberately awkward database.
- **Not safe against a production database.** A read-only connection plus a
  statement filter is enough for a sample file, not for real customer data.
- **Not a recovery demo, as it turns out.** The recovery path is implemented
  and tested, and measured at 0% on this schema with these models. Reporting
  that is more useful than engineering a run that trips it.

## Licence

[MIT](LICENSE).

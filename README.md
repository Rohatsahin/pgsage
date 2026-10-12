<p align="center">
  <img src="assets/logo.svg" width="128" height="128" alt="pgsage logo: a query plan tree with one proven branch">
</p>

<h1 align="center">pgsage</h1>

<p align="center">
  <strong>An LLM proposes. Postgres proves.</strong><br>
  AI-native PostgreSQL query tuning where every suggestion is measured on a real database before you see it.
</p>

<p align="center">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776ab">
  <img alt="Tested on PostgreSQL 17" src="https://img.shields.io/badge/tested%20on-postgresql%2017-336791">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Typed: pyright strict" src="https://img.shields.io/badge/types-pyright%20strict-informational">
  <img alt="MCP server included" src="https://img.shields.io/badge/MCP-server%20included-8a63d2">
</p>

---

## Why

Asking an AI "why is this query slow?" gives you confident prose that is often wrong: indexes the
planner ignores, rewrites that quietly change the results, advice that does nothing. pgsage keeps
the model for what it is good at (reading a plan, spotting the bottleneck, proposing a fix) and
hands the verdict to PostgreSQL.

```text
plan + table stats ─► LLM proposes ─► allow-list ─► apply in a rolled-back transaction ─► measure
        ▲                index / statistics / rewrite                                     │
        └──────────────── what failed, and why, goes back to the model ◄─────────────────┘
```

- 🧪 **Proven, not claimed.** Changes are applied inside a transaction that is rolled back and
  timed (best of 3) before and after. No speedup, no report.
- 🔒 **Safe by construction.** One read-only statement, read-only transactions for every
  measurement, quoted identifiers, timeouts, and an allow-list on what the model may touch.
- 🛡️ **Prompt-injection aware.** Hostile table or column names never reach the model; literals are
  redacted and restored locally.
- 🤖 **Agent-ready.** An MCP server lets Claude Code or any agent run the whole loop.
- 📝 **Readable prompt.** The system prompt is a markdown file you can read and change:
  [`system.md`](src/pgsage/prompts/system.md).
- 🔌 **Any model.** Claude by default (structured output, prompt caching, effort control), or any
  OpenAI-compatible server such as Ollama or vLLM.

## Quick start

```bash
pip install 'pgsage[anthropic]'
export ANTHROPIC_API_KEY=...

pgsage tune --dsn postgresql://user:pw@scratch-host/db \
            --sql "SELECT * FROM orders WHERE customer_id = 7"
```

Use a **scratch or dev database**: tuning runs `EXPLAIN ANALYZE`, which executes the query.

`pgsage verify` shows what a proof looks like (a real run on a 300k-row table):

```text
[PROVEN] index: CREATE INDEX ON "orders" ("customer_id")
    cost 4845.9 -> 1417.6, 14.095 ms -> 1.165 ms, 12.1x faster
```

And `pgsage eval --oracle` runs the built-in cases with known-good answers:

```text
PASS  missing-index: proven index: CREATE INDEX ON "ev_orders" ("customer_id") (47.9x)
PASS  composite-index: proven index: CREATE INDEX ON "ev_orders" ("customer_id", "status") (42.5x)
PASS  date-cast-rewrite: proven rewrite: SELECT * FROM ev_events WHERE created_at >= ... (54.4x)
PASS  primary-key-lookup: no changes proposed
PASS  full-table-aggregate: no changes proposed
```

## Commands

| Command | What it does |
| --- | --- |
| `pgsage tune` | fetch plan and table stats, ask the model, prove each proposal, retry with feedback |
| `pgsage tune --apply-dir db/migration` | also write proven changes as Flyway-style `V<timestamp>__*.sql` files |
| `pgsage explain [plan.json]` | explain an `EXPLAIN (ANALYZE, FORMAT JSON)` you already have (no database needed) |
| `pgsage verify` | prove one index you chose yourself |
| `pgsage top` | the costliest `SELECT`s from `pg_stat_statements` |
| `pgsage diff old.json new.json` | exit 1 if the plan shape changed (regression check for CI) |
| `pgsage eval` | score a model on cases with known fixes (`--oracle` validates the harness, no LLM) |

Options for the model: `--provider anthropic|openai`, `--model`, `--effort low..max`,
`--base-url` (for OpenAI-compatible servers), `--rounds`.

## Use it from an AI agent

```bash
pip install 'pgsage[mcp]'
claude mcp add pgsage -e PGSAGE_DSN=postgresql://pgsage:...@scratch-host/db -- pgsage-mcp
```

Six tools (`get_top_queries`, `get_query_plan`, `get_table_info`, `summarize_plan`,
`verify_index_suggestion`, `verify_query_rewrite`). The DSN is fixed in the server's environment;
no tool accepts one. Details: [docs/mcp.md](docs/mcp.md).

## What "proven" means

| Change | Proven when |
| --- | --- |
| index (btree, partial, covering, gin, brin, hash) | the new plan uses it and is at least 1.1x faster |
| rewrite | same column names and types, same rows (in order if there is an `ORDER BY`), and faster |
| extended statistics | at least 1.1x faster, with `ANALYZE` run on both sides |

Rewrites that cannot be compared (volatile functions, `LIMIT` without `ORDER BY`) are never
proven. A proposal that fails on the database is reported as not proven, with the error fed back
to the model. Verification measures reads only; it does not measure the write cost of a new index.

## Safety, briefly

The real boundary is the database role: **use a least-privilege role on a scratch database.**
On top of that, pgsage accepts one read-only statement, runs every measurement in a read-only
transaction with timeouts, quotes all identifiers, aliases hostile names before they reach the
model, and rejects any proposal that targets something outside the query. What it does and does
not protect against: [docs/security.md](docs/security.md).

## Documentation

| | |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | data flow, the proof, modules, prompt layout and caching |
| [docs/security.md](docs/security.md) | threat model, what is sent to the LLM, residual risks, role setup |
| [docs/mcp.md](docs/mcp.md) | agent setup, tools, a typical loop |
| [src/pgsage/prompts/system.md](src/pgsage/prompts/system.md) | the system prompt, with worked examples |
| [AGENTS.md](AGENTS.md) | conventions and hard invariants for AI coding agents and contributors |
| [CONTRIBUTING.md](CONTRIBUTING.md) | how to contribute and report a vulnerability |

## Development

```bash
make install     # 📦 uv sync --all-extras
make check       # ✅ ruff + format + pyright (strict) + pytest, with an emoji summary
```

Tests start a real `postgres:17` through Docker; the LLM is always faked, so no API key is needed.
CI runs the same checks on Python 3.12 and 3.13 and publishes a job summary with per-file results
and the slowest tests.

## License

MIT

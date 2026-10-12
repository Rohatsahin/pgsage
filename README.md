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
- 🛡️ **Prompt-injection aware.** Unusual table and column names (spaces, `;`, instruction-like
  text) are aliased before the model sees them, and literals are redacted and restored locally;
  see the security doc for what plain-looking names mean.
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

### Large queries

You do not have to squeeze a query into a quoted string:

```bash
pgsage tune --dsn ... --file slow_query.sql        # from a file
cat slow_query.sql | pgsage tune --dsn ...         # from stdin (or: --file -)
pgsage tune --dsn ... --edit                       # write it in your $EDITOR
pgsage tune --dsn ...                              # in a terminal with no source: opens $EDITOR
pgsage tune --dsn ... --file q.sql --dry-run       # size of the prompt, no model call
pgsage tune --dsn ... --file q.sql --show-prompt   # the exact prompt that would be sent
```

For example, with `slow_query.sql` holding a small multi-line query against a 300k-row table
(real output):

```text
$ pgsage tune --dsn postgresql://... --file slow_query.sql --dry-run
Prompt built; nothing was sent to a model.
  system prompt: 16,317 characters (static, cached)
  query, plan and tables: 3,265 characters (cached across rounds)
  about 4,931 tokens in total (estimate: 1 token per 4 characters)
  plan: 2 nodes, sent in full; tables: 1
```

`--show-prompt` prints the same summary and then the exact prompt, which starts like this
(literals are already tokens such as `__L1__`, and the boundary token changes every run):

```text
--- prompt (after the system prompt) ---
Boundary token: 22f4cdeb5c045dc8
UNTRUSTED INPUT BEGINS [22f4cdeb5c045dc8]
{"plan":{"Plan":{"Node Type":"Gather","Startup Cost":1000.0, ...
```

Giving two sources at once is refused:

```text
$ pgsage tune --dsn ... --sql "SELECT 1" --file slow_query.sql
error: use only one of --sql, --file and --edit
```

`verify` takes the same `--sql`, `--file` and `--edit`. The prompt is built to stay small as the
query grows: JSON is sent compact (indentation scales with plan depth), the query is
whitespace-normalized, and a plan with more than 60 nodes sends a per-node digest plus the full
detail of the 8 costliest nodes instead of the whole tree. On a synthetic left-deep 100-join plan
this took the prompt from about 240,000 tokens to about 8,000. A query over 100,000 characters
is refused with a clear message, never silently truncated.

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
| `pgsage tune` | fetch plan and table stats, ask the model, prove each proposal, retry with feedback; the query comes from `--sql`, `--file`, `--edit` or stdin |
| `pgsage tune --dry-run` / `--show-prompt` | build the prompt, print its size (or the whole prompt), call no model |
| `pgsage tune --report-dir reports/` | also write a Markdown report, a JSON report and a plain SQL script of the proven changes (any migration tool, or `psql -f`) |
| `pgsage explain [plan.json]` | explain an `EXPLAIN (ANALYZE, FORMAT JSON)` you already have, no database needed; the advice is **unverified**, use `tune` to prove it |
| `pgsage verify` | prove one index you chose yourself |
| `pgsage top` | the costliest `SELECT`s from `pg_stat_statements` |
| `pgsage eval` | score a model on cases with known fixes (`--oracle` validates the harness, no LLM) |

Options for the model: `--provider anthropic|openai`, `--model`, `--effort low..max`,
`--base-url` (for OpenAI-compatible servers), `--rounds`.

## The report

`pgsage tune --report-dir reports/` writes three tool-neutral files per run (timestamped, never
overwriting an earlier run):

| File | For | Contains |
| --- | --- | --- |
| `pgsage-<time>.md` | people | the query, baseline and hotspots, the diagnosis with evidence, a results table, each proven change with its measured before and after, what was not proven and why, and caveats |
| `pgsage-<time>.json` | tools and CI | the same data, versioned (`schema_version`), including per-change `proven`, `speedup`, `sql` and `error` |
| `pgsage-<time>.sql` | applying | only the proven DDL, idempotent (`IF NOT EXISTS`), indexes built `CONCURRENTLY` (`--no-concurrently` to turn that off); paste into any migration tool or run `psql -f` |

Rewrites appear in the report with their equivalence verdict but not in the SQL script, since
they belong in your application code. The report never contains connection details, and model
written text is escaped before it goes into the Markdown.

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
| index (btree, partial, covering, gin, brin, hash) | the new plan uses it, is at least 1.1x faster and at least 0.5 ms faster (partitioned parents work) |
| rewrite | same column names, types and precision, same rows (in order if there is a top-level `ORDER BY`), and faster in interleaved runs; a rewrite identical to the original is not a proof |
| extended statistics | at least 1.1x and 0.5 ms faster, with `ANALYZE` run on both sides |

Rewrites that cannot be compared (volatile functions, `LIMIT` without `ORDER BY`) are never
proven. A proposal that fails on the database is reported as not proven, with the error fed back
to the model. Verification measures reads only; it does not measure the write cost of a new index.

## Safety, briefly

The real boundary is the database role: **use a least-privilege role on a scratch database.**
On top of that, pgsage accepts one read-only statement, runs every measurement in a read-only
transaction with timeouts, quotes all identifiers, has PostgreSQL itself refuse any second
statement, aliases unusual names before they reach the model, and rejects any proposal that
targets something outside the query. What it does and does
not protect against: [docs/security.md](docs/security.md).

## Documentation

| | |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | data flow, the proof, modules, prompt layout and caching |
| [docs/security.md](docs/security.md) | threat model, what is sent to the LLM, residual risks, role setup |
| [docs/mcp.md](docs/mcp.md) | agent setup, tools, a typical loop |
| [src/pgsage/prompts/system.md](src/pgsage/prompts/system.md) | the system prompt, with worked examples |
| [SECURITY.md](SECURITY.md) | how to report a vulnerability, and what counts |
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

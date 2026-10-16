<p align="center">
  <img src="assets/logo.svg" width="128" height="128" alt="pgsage logo: a query plan tree with one proven branch">
</p>

<h1 align="center">pgsage</h1>

<p align="center">
  <strong>Your agent proposes. Postgres proves.</strong><br>
  Paste a slow query to your AI coding agent. pgsage gives it the plan facts and measures every fix on a real database before the agent tells you about it.
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
planner ignores, rewrites that quietly change the results, advice that does nothing. Your agent
is good at reading a plan and proposing a fix. It cannot tell whether the fix works. pgsage can:
it applies each proposal inside a transaction that is rolled back (statistics proofs aside, see below), times the query before and
after, and returns a verdict.

```text
you paste a slow query
        │
        ▼
 your agent ──► pgsage inspect ──► plan digest + table facts
        │
        ├──────► pgsage verify ──► apply in a rolled-back transaction, measure ──► proven or not
        │             ▲                                                              │
        └─────────────┴──────── the agent reads the verdict and tries something else ┘
```

- 🧪 **Proven, not claimed.** Best of three timed runs before and after. No measured speedup, no
  recommendation.
- 🤖 **Built for agents.** An MCP server and a thin command line that prints JSON, plus a skill
  that teaches the agent how to read a plan and what is safe to propose.
- 🧠 **No model inside.** pgsage never calls an LLM and needs no API key. Your agent is the model.
- 🔒 **Safe by construction.** One read-only statement, read-only transactions for every
  measurement, quoted identifiers, timeouts, and a `protected` flag for databases it must never
  touch.

## Quick start

```bash
pip install 'pgsage[mcp]'

pgsage init                     # writes a starter pgsage.toml; edit the connection
pgsage connections test         # check you can reach it

pgsage install-skill            # teach your agent (Claude Code: .claude/skills/pgsage)
claude mcp add pgsage -e PGSAGE_CONNECTION=scratch -e PGSAGE_SCRATCH_PASSWORD -- pgsage-mcp   # optional: MCP tools
```

Then paste a slow query to your agent and ask why it is slow. Use a **scratch or dev database**:
the tools run `EXPLAIN ANALYZE`, which executes the query.

The skill and the MCP server do the same job; use either or both. An agent with only a shell
uses the commands directly, which is how the transcript below was produced.

### Shell or MCP

An agent with a shell calls the `pgsage` commands below. An agent with MCP gets seven tools that
do the same work: `get_top_queries`, `get_query_plan`, `summarize_plan`, `get_table_info`,
`verify_index_suggestion`, `verify_statistics_suggestion` and `verify_query_rewrite`. The database
is fixed in the server's environment; no tool accepts a connection string. Details:
[docs/agents.md](docs/agents.md) and [docs/mcp.md](docs/mcp.md).

### What the agent sees

You: *"This is slow, can you fix it?"*
`SELECT * FROM orders WHERE customer_id = 7 AND status = 'status2'`

The agent runs `pgsage inspect` and gets the plan digest and table facts as JSON (shortened):

```json
{
  "query": "SELECT * FROM orders WHERE customer_id = 7 AND status = 'status2'",
  "baseline_ms": 19.569,
  "digest": [
    {"depth": 0, "node": "Gather", "self_ms": 3.656, "estimate_off_by": 5.0},
    {"depth": 1, "node": "Seq Scan", "relation": "orders", "actual_rows_total": 300.0,
     "rows_removed_by_filter": 299700.0, "self_ms": 15.834, "estimate_off_by": 4.3}
  ],
  "tables": [
    {"table": "public.orders", "indexes": [], "size": {"estimated_rows": 300000},
     "column_stats": [{"column_name": "customer_id", "n_distinct": 1000.0}]}
  ]
}
```

The Seq Scan reads 300,000 rows and throws away 299,700, and `orders` has no index. The agent
proposes one and proves it with `pgsage verify index --table orders --columns customer_id,status`:

```json
{
  "kind": "index",
  "statement": "CREATE INDEX ON \"orders\" (\"customer_id\", \"status\")",
  "before": {"execution_ms": 7.713, "node_types": ["Gather", "Seq Scan"], "uses_new_index": false},
  "after": {"execution_ms": 0.112, "node_types": ["Bitmap Heap Scan", "Bitmap Index Scan"],
            "uses_new_index": true},
  "apply": ["CREATE INDEX CONCURRENTLY IF NOT EXISTS \"idx_orders_customer_id_status\" ON \"orders\" (\"customer_id\", \"status\");"],
  "improved": true,
  "speedup": 68.87
}
```

Agent: *"`orders` is scanned in full and almost every row is discarded. An index on
`(customer_id, status)` cut the query from 7.7 ms to 0.1 ms in a measured, rolled-back trial.
To apply it: `CREATE INDEX CONCURRENTLY IF NOT EXISTS ...`. It will also add cost to every write
on `orders`."*

That output is from a real run on a 300,000-row table; timings vary with your machine and data.
A change that does not help comes back with `"improved": false` and the reason, and the agent
tries something else.

### Connections

Name your databases once instead of pasting connection strings. `pgsage.toml` (found by walking
up from the current directory, so it can live in the repo) and `~/.config/pgsage/config.toml`
(yours alone) both work; the project file overrides yours by name.

```toml
default_connection = "scratch"

[connections.scratch]
host = "localhost"
dbname = "app"
user = "app"
password_env = "PGSAGE_SCRATCH_PASSWORD"

[connections.ci]
dsn = "${PGSAGE_DATABASE_URL}"

[connections.staging-clone]
service = "staging_clone"
protected = true
```

```bash
pgsage inspect -c staging-clone --file q.sql   # -c / --connection picks a name
pgsage connections                             # list names, targets and where they come from
```

The database is chosen in this order: `--dsn` (or `PGSAGE_DSN`), `--connection` (or
`PGSAGE_CONNECTION`), `default_connection`, then the standard `PGHOST`, `PGSERVICE` and friends,
so `~/.pg_service.conf` and `~/.pgpass` work as usual.

- **No passwords in files.** A `password` key is rejected. Use `password_env`, `${VAR}` inside a
  `dsn`, a `.pgpass` file, or `password_command = ["op", "read", "op://vault/db/password"]`.
  `password_command` runs a program (no shell), so it is only honored in your own user config,
  never in a project file you may have just cloned. For the same reason a project file may only
  read environment variables that start with `PGSAGE_`, and it cannot redefine a connection your
  user config already defines.
- **`protected = true`** marks a database where pgsage must never create indexes or run your
  query to prove a change. The `verify` commands and tools refuse it; `inspect` and `top` still
  work, and `inspect` does run the query (`EXPLAIN ANALYZE`). Only named connections can be
  protected: a `--dsn`, `PGSAGE_DSN` or the `PG*` variables are never.
- Behind a bastion? Open the tunnel yourself (`ssh -L 5433:db:5432 bastion`) and point `host` and
  `port` at it. pgsage does not manage tunnels.
- A password in `--dsn` shows up in `ps`; prefer a named connection.

## Commands

Every command prints one JSON document on stdout; messages go to stderr; failures print
`error: ...` and exit 2.

| Command | What it does |
| --- | --- |
| `pgsage inspect` | plan digest, baseline time and table facts for a `SELECT` (`--raw-plan` adds the raw plan) |
| `pgsage verify index` | prove an index (`--method`, `--include`, `--where`); exit 1 if not proven |
| `pgsage verify statistics` | prove extended statistics (`--kinds`); exit 1 if not proven |
| `pgsage verify rewrite` | prove a rewritten `SELECT` returns the same rows, faster (`--rewrite` or `--rewrite-file`); exit 1 if not proven |
| `pgsage top` | the costliest `SELECT`s from `pg_stat_statements` (values appear as `$n`) |
| `pgsage connections` / `connections test` | list named connections; connect and report who and what you reached |
| `pgsage init` | write a starter `pgsage.toml` (never overwrites) |
| `pgsage install-skill` | install the skill for your agent (`--user` for every project, `--dir` for another agent) |

The query comes from `--sql`, `--file` (`-` for stdin) or piped stdin; nothing ever prompts.
Every database command takes `-c/--connection NAME` or `--dsn`.

## What "proven" means

| Change | Proven when |
| --- | --- |
| index (btree, partial, covering, gin, brin, hash) | the new plan uses it and is at least 1.1x and 0.5 ms faster (partitioned parents work) |
| rewrite | same column names, types and precision, same rows (in order if there is a top-level `ORDER BY`), and faster in interleaved runs; a rewrite identical to the original is not a proof |
| extended statistics | at least 1.1x and 0.5 ms faster, with `ANALYZE` run on both sides |

Rewrites that cannot be compared (volatile functions, `LIMIT` without `ORDER BY`) are never
proven. A proposal that fails on the database comes back as not proven with the error, so the
agent can correct it. Verification measures reads only; it does not measure the write cost of a
new index. For a proven change, `apply` holds idempotent SQL (indexes use `CONCURRENTLY`, which
must run outside a transaction block).

## Safety, briefly

The real boundary is the database role: **use a least-privilege role on a scratch database.**
On top of that, pgsage accepts one read-only statement, runs every measurement in a read-only
transaction with timeouts, quotes all identifiers, has PostgreSQL itself refuse any second
statement, and never takes a connection string from a tool call. Your agent sees what the tools
return: names, plan conditions, literals and error text from your database. What pgsage does and
does not protect against: [docs/security.md](docs/security.md).

## Documentation

| | |
| --- | --- |
| [docs/agents.md](docs/agents.md) | using pgsage from an AI agent: MCP or shell, the loop, the JSON contract, exit codes |
| [docs/mcp.md](docs/mcp.md) | MCP setup and tools |
| [docs/architecture.md](docs/architecture.md) | data flow, the proof, modules |
| [docs/security.md](docs/security.md) | threat model, residual risks, role setup |
| [src/pgsage/skill/SKILL.md](src/pgsage/skill/SKILL.md) | the skill: how an agent reads a plan and what it may propose |
| [SECURITY.md](SECURITY.md) | how to report a vulnerability, and what counts |
| [AGENTS.md](AGENTS.md) | conventions and hard invariants for AI coding agents and contributors |
| [CONTRIBUTING.md](CONTRIBUTING.md) | how to contribute |

## Development

```bash
make install     # 📦 uv sync --all-extras
make check       # ✅ ruff + format + pyright (strict) + pytest, with an emoji summary
```

Tests start a real `postgres:17` through Docker; no model and no API key are involved.
CI runs the same checks on Python 3.12, 3.13 and 3.14 and publishes a job summary with per-file results
and the slowest tests.

## License

MIT

<p align="center">
  <img src="https://raw.githubusercontent.com/Rohatsahin/pgsage/main/assets/logo.svg" width="128" height="128" alt="pgsage logo: a query plan tree with one proven branch">
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
it applies each proposal inside a transaction that is rolled back, times the query before and
after, and returns a verdict. (One exception: a statistics proof runs `ANALYZE`, which refreshes
the table's row-count estimates for good; see "Safety, briefly".)

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

- 🧪 **Measured, not claimed.** Best of five timed runs before and after, with a speedup bar that
  rises with the timing noise it observes. No measured speedup, no recommendation.
- 🤖 **Built for agents.** An MCP server and a thin command line that prints JSON, plus a skill
  that teaches the agent how to read a plan and what is safe to propose.
- 🧠 **No model inside.** pgsage never calls an LLM and needs no API key. Your agent is the model.
- 🔒 **Safe by construction.** One read-only statement, read-only transactions for every
  measurement, quoted identifiers, timeouts, and a `protected` flag for databases it must never
  touch.

## Quick start

Try it on a throwaway database in about two minutes:

```bash
docker run -d --rm --name pgsage-demo -p 5432:5432 -e POSTGRES_PASSWORD=pw postgres:17
docker exec pgsage-demo psql -U postgres -c "CREATE TABLE orders AS SELECT g AS id, g % 1000 AS customer_id, 'status' || (g % 5) AS status, now() - (g || ' minutes')::interval AS created_at FROM generate_series(1, 300000) g; ANALYZE orders"

pip install 'pgsage[mcp]'                 # from a clone: pip install '.[mcp]'
export PGSAGE_SCRATCH_PASSWORD=pw
pgsage init                               # writes pgsage.toml (postgres on localhost:5432)
pgsage connections test                   # check you can reach it

pgsage inspect --sql "SELECT * FROM orders WHERE customer_id = 7"
pgsage verify index --table orders --columns customer_id --sql "SELECT * FROM orders WHERE customer_id = 7"
```

Then wire it to your agent, so you can just paste a slow query and ask why it is slow:

```bash
pgsage install-skill      # teaches the agent (Claude Code: .claude/skills/pgsage); --dir for others
claude mcp add pgsage -e PGSAGE_CONNECTION=scratch -- pgsage-mcp     # optional: MCP tools
```

Keep the password out of the agent's configuration: leave `password_env` out of the connection and
put the password in `~/.pgpass` (libpq reads it), or use `password_command` in your user config
(`pgsage init --user`). `pgsage-mcp` must be on the `PATH` that your agent sees; a virtualenv or
pipx install usually is not, so give the full path.

Use a **scratch or dev database**: the tools run `EXPLAIN ANALYZE`, which executes the query, and
proofs create indexes (see "Safety, briefly" for the role you need).

The skill and the MCP server do the same job; use either or both. An agent with only a shell
uses the commands directly, which is how the transcript below was produced.

### Shell or MCP

An agent with a shell calls the `pgsage` commands below. An agent with MCP gets eight tools that
do the same work: `get_top_queries`, `get_query_plan`, `summarize_plan`, `get_table_info`,
`verify_index_suggestion`, `verify_statistics_suggestion`, `verify_setting_suggestion` and
`verify_query_rewrite`. The database
is fixed in the server's environment; no tool accepts a connection string. Details:
[docs/agents.md](https://github.com/Rohatsahin/pgsage/blob/main/docs/agents.md) and [docs/mcp.md](https://github.com/Rohatsahin/pgsage/blob/main/docs/mcp.md).

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
  "reason": null,
  "speedup": 68.87
}
```

Agent: *"`orders` is scanned in full and almost every row is discarded. An index on
`(customer_id, status)` cut the query from 7.7 ms to 0.1 ms in a measured, rolled-back trial.
To apply it: `CREATE INDEX CONCURRENTLY IF NOT EXISTS ...`. It will also add cost to every write
on `orders`."*

The two JSON blocks are from a real run on a 300,000-row table (shortened), and the agent's reply
is illustrative. `baseline_ms` is one cold run of the query; `before` and `after` are the best of
five runs, which is why they differ. Timings vary with your machine and data. A change that does
not help comes back with `"improved": false` and a `reason`, and the agent tries something else.

### Connections

Name your databases once instead of pasting connection strings. `pgsage.toml` (found by walking
up from the current directory, so it can live in the repo) and `~/.config/pgsage/config.toml`
(yours alone) both work. The project file is searched from the current directory up to the
repository root (never your home directory) and can only add connections: it cannot redefine a
connection from your user config.

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
  name a local host (`localhost`, `127.0.0.1`, `::1` or a socket directory), may only read
  environment variables that start with `PGSAGE_`, cannot set `service` or `options`, cannot hold
  a password inside a `dsn`, and cannot change your default connection. Anything remote belongs in
  your user config.
- **`protected = true`** marks a database where pgsage must never create indexes or run your
  query to prove a change. The `verify` commands and tools refuse it; `inspect` and `top` still
  work, and `inspect` does run the query (`EXPLAIN ANALYZE`). Only named connections can be
  protected: a `--dsn`, `PGSAGE_DSN` or the `PG*` variables are never.
- Behind a bastion? Open the tunnel yourself (`ssh -L 5433:db:5432 bastion`) and point `host` and
  `port` at it. pgsage does not manage tunnels.
- A password in `--dsn` shows up in `ps`; prefer a named connection.

## Commands

Commands that query the database print one JSON document on stdout; messages go to stderr;
failures print `error: ...` and exit 2 (a usage mistake gets the argument parser's own message,
also exit 2).

| Command | What it does |
| --- | --- |
| `pgsage inspect` | plan digest, server settings and table facts for a `SELECT` (`--raw-plan` adds the raw plan; `--no-analyze` estimates without running the query) |
| `pgsage summarize` | the digest of an `EXPLAIN (ANALYZE, FORMAT JSON)` plan you already have; no database needed |
| `pgsage verify index` | prove an index (`--method`, `--include`, `--where`); exit 1 if not proven |
| `pgsage verify statistics` | prove extended statistics (`--kinds`); exit 1 if not proven |
| `pgsage verify setting` | prove a settings change for this session only (`--set work_mem=64MB`, `jit=off`, ...); exit 1 if not proven |
| `pgsage verify rewrite` | prove a rewritten `SELECT` returns the same rows, faster (`--rewrite` or `--rewrite-file`); exit 1 if not proven |
| `pgsage top` | the costliest `SELECT`s from `pg_stat_statements` (values appear as `$n`) |
| `pgsage connections` / `connections test` | list named connections; connect and report who and what you reached (`connections test NAME` or `-c NAME`) |
| `pgsage init` | write a starter `pgsage.toml` (never overwrites) |
| `pgsage install-skill` | install the skill for your agent (`--user` for every project, `--dir` for another agent) |

The query comes from `--sql`, `--file` (`-` for stdin) or piped stdin; nothing ever prompts.
Every database command takes `-c/--connection NAME` or `--dsn`.

## Good fit, not a fit

- **A good fit:** a slow `SELECT` on a PostgreSQL scratch or staging copy, where you want an index,
  a rewrite, extended statistics or a settings change that is measured before anyone recommends it.
- **Not a fit:** writes (`INSERT`, `UPDATE`, `DELETE`), non-PostgreSQL databases, queries that run
  longer than about 10 s (each proof runs the query about ten times under a 60 s limit; use
  `inspect --no-analyze` to diagnose from estimates), or measuring the write cost of an index.
- **Limits you should know:** only PostgreSQL 17 is tested; a speedup is for one machine, one
  dataset and one moment; a rewrite is compared on the current data, not proven for all data.

## What "proven" means

| Change | Proven when |
| --- | --- |
| index (btree, partial, covering, gin, brin, hash) | the new plan uses it and is faster by the bar below (partitioned parents work) |
| rewrite | same column names, types and precision, same rows (in order if there is a top-level `ORDER BY`), and faster in interleaved runs; a rewrite identical to the original is not a proof |
| extended statistics | faster by the bar below **and** a changed plan, compared against two runs of plain `ANALYZE` (a plan that already changes between those two is sampling noise and is refused); the weakest proof, and often not proven |
| setting | faster by the bar below with the value applied to this session only, measured alternating with the default; the plan does not have to change and nothing about results is compared |

**The bar:** at least 0.5 ms faster and at least 1.1x, where the 1.1x rises to `1 + 3 x noise` when
the repeated runs disagree with each other. A pass under 1.3x is run a second time and must pass
again. Per-node timing is switched off during measurement, so the instrumentation does not bend the
ratio. In testing against a quiet PostgreSQL 17 host, changes that cannot help passed about 1% of
the time; on a host with its CPUs saturated by other work they passed far more often, so measure on
a quiet database and treat anything under about 1.3x as weak evidence. Gains under 0.5 ms are never
proven, however large the ratio. `verify` also reports `planning_ms` next to `execution_ms`.

Rewrites that cannot be compared (volatile functions, `LIMIT` without `ORDER BY`) are never
proven. A proposal that fails on the database comes back as not proven with the error, so the
agent can correct it. Verification measures reads only; it does not measure the write cost of a
new index. For a proven change, `apply` holds idempotent SQL (indexes use `CONCURRENTLY`, which
must run outside a transaction block).

## Safety, briefly

The real boundary is the database role: **use a dedicated role on a scratch copy of the data.**
`CREATE INDEX` needs table ownership, so the role must own the tables it proves against (not an
application or superuser role; see [docs/security.md](https://github.com/Rohatsahin/pgsage/blob/main/docs/security.md)).
On top of that, pgsage accepts one read-only statement, runs every measurement in a read-only
transaction with timeouts, quotes all identifiers, has PostgreSQL itself refuse any second
statement, and never takes a connection string from a tool call. Your agent sees what the tools
return: names, plan conditions, literals and error text from your database. What pgsage does and
does not protect against: [docs/security.md](https://github.com/Rohatsahin/pgsage/blob/main/docs/security.md).

## Documentation

| | |
| --- | --- |
| [docs/agents.md](https://github.com/Rohatsahin/pgsage/blob/main/docs/agents.md) | using pgsage from an AI agent: MCP or shell, the loop, the JSON contract, exit codes |
| [docs/mcp.md](https://github.com/Rohatsahin/pgsage/blob/main/docs/mcp.md) | MCP setup and tools |
| [docs/architecture.md](https://github.com/Rohatsahin/pgsage/blob/main/docs/architecture.md) | data flow, the proof, modules |
| [docs/security.md](https://github.com/Rohatsahin/pgsage/blob/main/docs/security.md) | threat model, residual risks, role setup |
| [src/pgsage/skill/SKILL.md](https://github.com/Rohatsahin/pgsage/blob/main/src/pgsage/skill/SKILL.md) | the skill: how an agent reads a plan and what it may propose |
| [SECURITY.md](https://github.com/Rohatsahin/pgsage/blob/main/SECURITY.md) | how to report a vulnerability, and what counts |
| [AGENTS.md](https://github.com/Rohatsahin/pgsage/blob/main/AGENTS.md) | conventions and hard invariants for AI coding agents and contributors |
| [CONTRIBUTING.md](https://github.com/Rohatsahin/pgsage/blob/main/CONTRIBUTING.md) | how to contribute |

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

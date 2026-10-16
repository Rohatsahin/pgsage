# AGENTS.md

Guidance for AI coding agents (and humans) working on pgsage. Read this before changing code.

pgsage is the toolset an AI coding agent uses to tune PostgreSQL queries: the agent proposes an
index, extended statistics or a query rewrite, and pgsage **proves it on a real database** (applied
inside a rolled-back transaction, measured before and after). pgsage contains no model and calls
none. Deep dives: `docs/architecture.md`, `docs/security.md`, `docs/agents.md`, `docs/mcp.md`, and
the skill in `src/pgsage/skill/SKILL.md`.

## Commands

Always go through `uv`. Docker must be running (the tests start `postgres:17` with testcontainers).

```bash
make check                      # lint + format + pyright + pytest, with an emoji summary
uv run pytest -q                # all tests (~15s)
uv run pytest tests/test_sql_gate.py -q     # one file
uv run pytest -m "not docker" -q            # everything that needs no Docker
uv run ruff check . && uv run ruff format .
uv run pyright                  # strict; covers src, tests, scripts
```

`pgsage-mcp` starts a stdio server and waits for input. Never run it in the foreground with
`--help`; it will hang.

## Layout

| Path | Job |
| --- | --- |
| `src/pgsage/plan.py` | typed `EXPLAIN (FORMAT JSON)` model, derived metrics, `plan_digest` |
| `src/pgsage/sqlscan.py` | the SQL lexer (comments, strings, dollar quotes, quoted identifiers) |
| `src/pgsage/verify.py` | the SQL safety gate, suggestion models, the proofs (`verify_*`) and the `apply` SQL |
| `src/pgsage/config.py` | named connections (`pgsage.toml`, user config), secrets, `protected` |
| `src/pgsage/context.py` | read-only plan, table and top-query lookups, and `inspect_query` |
| `src/pgsage/cli.py` | the JSON-only command line |
| `src/pgsage/mcp_server.py`, `mcp_main.py` | the MCP server; `mcp_main` is the `pgsage-mcp` launcher that explains a missing `mcp` extra |
| `src/pgsage/skill/SKILL.md` | the skill agents load: when to use the tools and the tuning guide |
| `scripts/` | CI summary generator and `check.sh` |

## Where tests go

Tests are organised by topic, one file per area. Add to the matching file; do not create a new
file per change or per review round.

| File | Covers |
| --- | --- |
| `test_sql_gate.py` | the lexer, `ensure_read_only`, fail-closed ambiguity, single-statement execution, the differential fuzz against PostgreSQL |
| `test_verify.py` | the proof (`improved`, `measure`), suggestions and their DDL, `apply` SQL, equivalence, partitions, read-only phases, timeouts |
| `test_context.py` | `get_plan`, `table_info`, ancestors, `top_queries`, `inspect_query` |
| `test_plan.py` | the `EXPLAIN` model, derived timings, parallel plans |
| `test_config.py` | connection config, secrets, `protected`, `init` and `connections` commands |
| `test_cli.py` | the CLI commands, query input (`--sql`, `--file`, stdin), JSON output, exit codes, `install-skill` |
| `test_mcp.py` | the MCP tools and launcher |
| `test_docs.py`, `test_ci_summary.py` | docs stay true to the code; the CI job-summary script |

Tests that need Docker are marked and skipped automatically when it is missing
(`conftest.pytest_collection_modifyitems`); `pytest -m "not docker"` runs the rest. Shared
fixtures (`pg_dsn`, `pg_conn`, `victim`, `stats_dsn`) live in `conftest.py`.

## Hard invariants (never weaken these)

These exist because an external review found real exploits. Each has regression tests.

1. **One read-only statement only, enforced twice.** `ensure_read_only` lexes the SQL; it rejects
   `;` outside literals, anything not starting with `SELECT/WITH/VALUES/TABLE`, DML/DDL/`DO`/
   `COMMIT`, `INTO`, `FOR UPDATE`, and known side-effect functions, and it **fails closed** on
   anything the lexer cannot read unambiguously (`is_ambiguous`). A regex over raw text is not a
   gate: always `mask()` first. The lexer must agree with PostgreSQL's (a lone `\r` ends a `--`
   comment, a dollar-quote tag cannot start with a digit); if you change it, add the case to
   `tests/test_sql_gate.py`. Independently, all caller-influenced SQL runs through `run_single`
   (`prepare=True`), so PostgreSQL itself rejects a second statement. Never call `conn.execute`
   on such text directly.
2. **Identifiers are quoted with `quote_ident`**, never interpolated bare. Names that reach DDL
   come from an agent that may be prompt-injected.
3. **Proof phases are read-only.** In `_experiment`, the baseline and the after-measurement run
   under `SET TRANSACTION READ ONLY`; only the `CREATE INDEX/STATISTICS` itself is read-write,
   and everything is rolled back. Every transaction sets `statement_timeout` and `lock_timeout`.
4. **No DSN from callers.** MCP tools and CLI commands take the database from the environment or
   the config (`PGSAGE_CONNECTION`, `PGSAGE_DSN`, `--connection`), never from a tool argument.
   Do not add a `dsn` parameter to a tool.
5. **`protected` connections are never proven against.** `verify` commands and the MCP verify
   tools refuse a connection marked `protected = true`.
6. **`password_command` runs a program, so it is honored only from the user config**, never from a
   project `pgsage.toml`, and it is run without a shell. A plaintext `password` key is rejected.
7. **An unproven change is never reported as proven.** `VerifyResult.improved` requires: no
   error, equivalence not false, the new index (or one of its partition children) actually used
   (for indexes), and a measured speedup of at least 1.1x **and** 0.5 ms. A rewrite identical to
   the original is never a proof. `apply` is only returned for a proven change.

## Conventions

- **No code comments** unless they carry a constraint the code cannot. No Arrange/Act/Assert
  labels in tests. Prose belongs in docs, not in source.
- Python 3.12, strict pyright, ruff. No `Any` leaking into public signatures where a model works.
- **Real Postgres in tests** (`pg_dsn` / `pg_conn` fixtures). pgsage has no model, so tests never
  need a key or a network call.
- **Fix tests, never delete them.** A broken test is a setup problem or a real bug. Do not weaken
  an assertion to make it pass. The one exception is a test of a feature that was deliberately
  removed.
- Prefer enriching what exists (a rule, a validator, a fixture) over adding a parallel mechanism.
- Timing-dependent assertions must not assert that something is *not* faster. Sub-millisecond
  queries make "not improved" flaky. Assert structure (error, equivalence, rollback) instead.
- Every command prints JSON on stdout and messages on stderr; exit `0` ran, `1` not proven
  (`verify`), `2` error. Keep that contract: agents parse it.
- Keep diffs small. No shipped scaffolding the task did not ask for.

## Recipes

**Change the skill.** Edit `src/pgsage/skill/SKILL.md`. It is the single source of the tuning
guide and ships in the wheel. Keep it in sync with the tools: if a command, flag, field or rule
changes, change the skill in the same diff. Do not put dates, ids or per-user text in it.

**Add a command or tool.** Four places move together: the function in `context.py` or `verify.py`,
the CLI command in `cli.py` (JSON on stdout, errors as `error: ...` with exit 2), the MCP tool in
`mcp_server.py` (same arguments, same result shape), and the skill and docs (`docs/agents.md`,
`docs/mcp.md`, README tables). `tests/test_docs.py` checks the tool and command lists match.

**Add a kind of proposal.** Model it like `IndexSuggestion` (validators, `ddl`, `suggested_name`),
add `verify_*` through `_experiment`, give it an `*_apply_sql` helper, then add the CLI subcommand,
the MCP tool, and a section in the skill. Write the failure tests first: bad names, injection,
rollback, read-only phases.

## Gotchas

- `VACUUM` the table before expecting an index-only scan; a fresh table has an empty visibility map.
- Statistics verification's `ANALYZE` is rolled back with the transaction **except** the row-count
  estimates in `pg_class` (`reltuples`, `relpages`), which PostgreSQL updates in place.
- Tests that need Docker skip cleanly without it (`conftest.pytest_collection_modifyitems`).
- Several tests share one session-scoped container; any test that creates tables or indexes must
  clean up in `try/finally`.
- `pgsage verify` and `inspect` execute queries (`EXPLAIN ANALYZE`). Use scratch databases.

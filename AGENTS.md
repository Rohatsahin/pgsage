# AGENTS.md

Guidance for AI coding agents (and humans) working on pgsage. Read this before changing code.

pgsage is AI-native PostgreSQL query tuning: an LLM proposes an index, extended statistics or a
query rewrite, and pgsage **proves it on a real database** (applied inside a rolled-back
transaction, measured before and after) before reporting it. Deep dives: `docs/architecture.md`,
`docs/security.md`, `docs/mcp.md`, and the prompt itself in `src/pgsage/prompts/system.md`.

## Commands

Always go through `uv`. Docker must be running (the tests start `postgres:17` with testcontainers).

```bash
make check                      # lint + format + pyright + pytest, with an emoji summary
uv run pytest -q                # all tests (~15s)
uv run pytest tests/test_sql_gate.py -q     # one file
uv run ruff check . && uv run ruff format .
uv run pyright                  # strict; covers src, tests, scripts
uv run pgsage eval --dsn <scratch-db> --oracle   # validate the eval harness without an LLM
```

`pgsage-mcp` starts a stdio server and waits for input. Never run it in the foreground with
`--help`; it will hang.

## Layout

| Path | Job |
| --- | --- |
| `src/pgsage/plan.py` | typed `EXPLAIN (FORMAT JSON)` model, derived metrics, `plan_digest` |
| `src/pgsage/sqlscan.py` | the SQL lexer (comments, strings, dollar quotes, quoted identifiers) |
| `src/pgsage/verify.py` | the SQL safety gate, suggestion models, and the proofs (`verify_*`) |
| `src/pgsage/context.py` | read-only plan/table/top-query lookups |
| `src/pgsage/ai.py` | redaction and aliasing, prompt building, response parsing, LLM clients |
| `src/pgsage/prompts/system.md` | the system prompt, loaded verbatim |
| `src/pgsage/schema.py` | JSON schema for structured output |
| `src/pgsage/tune.py` | the propose, validate, prove, retry loop |
| `src/pgsage/report.py` | tool-neutral output: Markdown report, JSON report, SQL script of proven changes |
| `src/pgsage/evals.py` | eval cases and the harness |
| `src/pgsage/cli.py`, `mcp_server.py`, `mcp_main.py` | the two front ends; `mcp_main` is the `pgsage-mcp` launcher that explains a missing `mcp` extra |
| `scripts/` | CI summary generator and `check.sh` |

## Hard invariants (never weaken these)

These exist because an external review found real exploits. Each has regression tests.

1. **One read-only statement only, enforced twice.** `ensure_read_only` lexes the SQL; it rejects
   `;` outside literals, anything not starting with `SELECT/WITH/VALUES/TABLE`, DML/DDL/`DO`/
   `COMMIT`, `INTO`, `FOR UPDATE`, and known side-effect functions, and it **fails closed** on
   anything the lexer cannot read unambiguously (`is_ambiguous`). A regex over raw text is not a
   gate: always `mask()` first. The lexer must agree with PostgreSQL's (a lone `\r` ends a `--`
   comment, a dollar-quote tag cannot start with a digit); if you change it, add the case to
   `tests/test_sql_gate.py`. Independently, all model- or caller-influenced SQL runs through
   `run_single` (`prepare=True`), so PostgreSQL itself rejects a second statement. Never call
   `conn.execute` on such text directly.
2. **Identifiers are quoted with `quote_ident`**, never interpolated bare. Names that reach DDL
   come from an LLM or an MCP caller.
3. **Proof phases are read-only.** In `_experiment`, the baseline and the after-measurement run
   under `SET TRANSACTION READ ONLY`; only the `CREATE INDEX/STATISTICS` itself is read-write,
   and everything is rolled back. Every transaction sets `statement_timeout` and `lock_timeout`.
4. **No DSN from callers.** MCP tools read `PGSAGE_DSN` from the server environment. Do not add
   a `dsn` parameter to a tool.
5. **Untrusted data stays untrusted, default-deny.** Everything from the database that goes into
   a prompt is sanitized: literals redacted, non-plain identifiers aliased to `__I<n>__` tokens,
   control characters stripped, lengths and counts capped. In plan JSON, a string field the code
   does not recognize is treated as SQL text and masked, and keys ending in `Name` are aliased;
   only an explicit vocabulary (`SAFE_VALUE_KEYS`) passes through. Free text never reaches the
   system prompt.
6. **A proposal must target this query.** `tune._allowlist_error` rejects tables and columns that
   are not in the plan's tables, before any database call. Model-controlled text (advisory `sql`)
   is printed, never executed.
7. **Restored values are re-validated.** `_restore_target` rebuilds suggestions through
   `model_validate`, so a restored partial-index predicate passes the safety checks again.
8. **An unproven change is never reported as proven.** `VerifyResult.improved` requires: no
   error, equivalence not false, the new index (or one of its partition children) actually used
   (for indexes), and a measured speedup of at least 1.1x **and** 0.5 ms. A rewrite identical to
   the original is never a proof.

## Conventions

- **No code comments** unless they carry a constraint the code cannot. No Arrange/Act/Assert
  labels in tests. Prose belongs in docs, not in source.
- Python 3.12, strict pyright, ruff. No `Any` leaking into public signatures where a model works.
- **Real Postgres in tests** (`pg_dsn` / `pg_conn` fixtures). LLMs are always faked
  (`tests/fakes.py::FakeClient`); tests never call a live API and need no key.
- **Fix tests, never delete them.** A broken test is a setup problem or a real bug. Do not weaken
  an assertion to make it pass.
- Prefer enriching what exists (a rule, a validator, a fixture) over adding a parallel mechanism.
- Timing-dependent assertions must not assert that something is *not* faster. Sub-millisecond
  queries make "not improved" flaky. Assert structure (error, equivalence, rollback) instead.
- Keep diffs small. No shipped scaffolding the task did not ask for.

## Recipes

**Change the prompt.** Edit `src/pgsage/prompts/system.md`. It is the single source of truth and
is loaded verbatim. Its three examples are validated against `EXPLANATION_SCHEMA` and the models
by `tests/test_prompt_file.py`; keep them valid. Do not put dates, ids or any per-request text in
it, or the prompt cache breaks (`test_prompt_is_static_so_the_cache_prefix_never_changes`).

**Add a field to the model output.** Four places move together: `src/pgsage/schema.py` (all keys
required, nullable via `_nullable`), the Pydantic `Action`/`Explanation` in `ai.py`, section 8 and
the examples in `system.md`, and the tests that parse them.

**Add a kind of proposal.** Model it like `IndexSuggestion` (validators, `ddl`, `suggested_name`),
add `verify_*` through `_experiment`, wire it in `tune._proposal`/`_verify_action`, allow-list it,
add it to the schema and prompt, then to `report.change_statements` if it is DDL. Write the failure tests first:
bad names, injection, rollback, read-only phases.

**Add an LLM provider.** Implement `LLMClient.complete(system, prompt)`. `prompt` may be a
`Prompt` (a `str` subclass with `.stable` and `.volatile`) if you want to place cache breakpoints.
Add it to `cli.make_client`. Test with a fake transport, as `OpenAICompatibleClient` does.

**Add an eval case.** Append to `evals.CASES` with a setup, a query, an expectation and an
oracle reply. `tests/test_evals_postgres.py` proves every oracle passes; check the case does not
depend on timing noise (look for at least a 2x effect).

## Prompt size

`build_prompt` keeps prompts small on purpose: compact JSON, a pruned plan above
`MAX_PLAN_NODES`, a capped digest, and a `MAX_QUERY_CHARS` refusal instead of truncation. Never
reintroduce `indent=` or silent truncation of the query: both were real bugs (indentation grows
with plan depth; a truncated query makes the model reason about SQL it was not given). Tests in
`tests/test_prompt_efficiency.py` pin this. `pgsage tune --dry-run` is the quick way to inspect.

## Gotchas

- `VACUUM` the table before expecting an index-only scan; a fresh table has an empty visibility map.
- Statistics verification's `ANALYZE` is rolled back with the transaction **except** the row-count
  estimates in `pg_class` (`reltuples`, `relpages`), which PostgreSQL updates in place.
- `from pgsage import tune` is the *function*; the module is `sys.modules["pgsage.tune"]` (use it
  when monkeypatching `table_info` and friends).
- Tests that need Docker skip cleanly without it (`conftest.pytest_collection_modifyitems`).
- The Anthropic request path (structured output, `cache_control`, fallbacks) is tested against a
  fake SDK only. After changing it, run `pgsage eval` once with a real key.
- Several tests share one session-scoped container; any test that creates tables or indexes must
  clean up in `try/finally`.
- `pgsage verify`, `tune` and `eval` execute queries (`EXPLAIN ANALYZE`). Use scratch databases.

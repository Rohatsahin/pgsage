# Architecture

pgsage has one idea: **the model proposes, Postgres proves.** The LLM never decides whether
something is good; it only suggests, and a measurement on the real database decides.

```mermaid
flowchart LR
    Q[SELECT query] --> P[get_plan<br/>read-only EXPLAIN ANALYZE]
    P --> C[table_info<br/>columns, indexes, stats]
    P --> B[build_prompt<br/>redact, alias, cap]
    C --> B
    B -->|cached prefix| L[LLM<br/>system.md + JSON schema]
    L --> R[parse and restore<br/>per-action validation]
    R --> A{allow-list<br/>table and columns<br/>in this plan?}
    A -- no --> F[failed result,<br/>fed back to the model]
    A -- yes --> V[verify<br/>apply, measure, roll back]
    V --> J{proven?}
    J -- yes --> M[report and<br/>Flyway migration]
    J -- no --> F
    F -->|next round| B
```

## The proof

`verify._experiment` is the heart. For one proposal it runs two transactions on one connection:

1. **Baseline.** `SET LOCAL` timeouts, optional `ANALYZE` (statistics only), then
   `SET TRANSACTION READ ONLY`, then the query three times; the fastest run is kept. Roll back.
2. **After.** Timeouts, apply the change (`CREATE INDEX` or `CREATE STATISTICS` + `ANALYZE`), then
   `SET TRANSACTION READ ONLY`, check equivalence (rewrites only), measure three times. Roll back.

`VerifyResult.improved` is true only if there is no error, the result is equivalent (or not
applicable), the new index is used (indexes), and the speedup is at least 1.1x (or the cost drops
when no timings exist).

Rewrites are compared on column names and types, then on rows as text (`EXCEPT ALL` both ways, or
an ordered hash when the query has `ORDER BY`). Volatile functions and `LIMIT` without
`ORDER BY` are never provable.

## The trust boundary

```text
 database ──(untrusted)──► sanitize ──► prompt ──► LLM ──(untrusted)──► validate ──► database
   names, values, text      alias, redact,        bounded JSON,        allow-list,
                            cap, delimit          per-action parse     re-validate, read-only
```

Data crosses the boundary twice and is treated as hostile both times. See `docs/security.md`.

## Modules

| Module | Responsibility |
| --- | --- |
| `plan.py` | Pydantic model of `EXPLAIN` JSON; `self_time` accounts for loops and parallel workers; `plan_digest` gives the model the derived numbers it computes badly |
| `sqlscan.py` | Lexer used by the safety gate, redaction and aliasing. The single place that knows SQL lexical structure |
| `verify.py` | `ensure_read_only`, `IndexSuggestion` / `StatsSuggestion`, `verify_index`, `verify_statistics`, `verify_rewrite`, and the equivalence check |
| `context.py` | Read-only lookups: `get_plan`, `table_info`, `top_queries` |
| `ai.py` | `Redaction` (literal tokens and name aliases), `build_prompt` (stable and volatile parts), `parse_explanation`, `explain_plan`, and the `AnthropicClient` / `OpenAICompatibleClient` |
| `prompts/system.md` | The system prompt; `prompts/__init__.py` loads it |
| `schema.py` | The JSON schema used for structured output |
| `tune.py` | Rounds, allow-list, attempt feedback; one `VerifiedChange` per proposal |
| `migrations.py` | Proven indexes and statistics as `V<timestamp>__<name>.sql` files, collision-proof |
| `evals.py` | Cases with known fixes, isolated per-case schemas, an oracle client |
| `shape.py` | Plan-shape fingerprint and diff, for regression checks |
| `cli.py`, `mcp_server.py` | Front ends; no logic of their own beyond wiring and printing |

## Prompt layout and caching

The request is ordered from most to least stable, so the provider can reuse its cache:

1. `system.md` (static; a cache breakpoint)
2. per-session boundary token, then the plan, digest, query and table metadata (a cache breakpoint)
3. `previous_attempts` and the closing reminder (not cached; changes every round)

The boundary token lives in `Redaction.nonce`, so it stays identical across rounds of one `tune`
call. A test (`test_cache_prefix_is_byte_identical_across_rounds`) guards this.

## Why these choices

- **Structured output** (`output_config.format`) so the reply is always parseable; parsing is still
  lenient and per-action, because the model can be refused or cut off.
- **A digest next to the raw plan**: self time with loops, and estimate error, are derived values
  models get wrong when asked to compute them from per-loop numbers.
- **Aliasing instead of dropping names**: the model needs table and column names to propose an
  index, so plain names stay readable and only unusual ones are tokenized and restored locally.
- **Best of three** measurements, a 1.1x threshold and an unused-index check: a single timing of
  a small query is noise, and an index the planner ignores is not a fix.

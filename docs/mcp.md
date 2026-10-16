# pgsage as an MCP server

The MCP server gives an agent (Claude Code, or any MCP client) the same operations as the
command line: find a slow query, read its plan and table stats, and prove a change. The agent does
the reasoning; the server does the measuring. See [agents](agents.md) for the full picture.

## Setup

```bash
pip install 'pgsage[mcp]'
claude mcp add pgsage -e PGSAGE_CONNECTION=scratch -- pgsage-mcp
```

Give the server a password without writing it into the client's configuration: leave
`password_env` out of the connection and use `~/.pgpass`, or `password_command` in your user
config. `pgsage-mcp` must be on the `PATH` that the client sees.

The server's own environment chooses the database: `PGSAGE_CONNECTION` (a name from your
`pgsage.toml` or user config), `PGSAGE_DSN`, or the `PG*` variables. A `protected` connection is
refused by the tools that run proofs. No tool accepts a connection string, so a prompt-injected
agent cannot redirect pgsage to another host or reuse your credentials. Use a dev or scratch
database and a least-privilege role (see [security](security.md)).

The skill (`pgsage install-skill`) is a good companion: the server tells the agent what the tools
do, and the skill tells it how to read a plan and what is safe to propose.

## Tools

| Tool | Arguments | Returns |
| --- | --- | --- |
| `get_top_queries` | `limit` | costliest `SELECT`s from `pg_stat_statements` (values appear as `$n`) |
| `get_query_plan` | `sql`, `analyze`, `raw_plan` | the per-node digest and server settings from a read-only `EXPLAIN ANALYZE` (`analyze` false for estimates only; `raw_plan` true adds the full plan) |
| `summarize_plan` | `plan_json` | the digest for `EXPLAIN` JSON you already have (no database needed) |
| `get_table_info` | `table`, `schema` | columns, indexes, size, `n_distinct`, `null_frac`, `correlation` |
| `verify_index_suggestion` | `sql`, `table`, `columns`, `schema`, `method`, `include`, `where` | before and after plans, `uses_new_index`, `improved`, `speedup`, `apply` |
| `verify_statistics_suggestion` | `sql`, `table`, `columns`, `kinds`, `schema` | before and after plans for `CREATE STATISTICS` + `ANALYZE`, `improved`, `apply` |
| `verify_setting_suggestion` | `sql`, `settings` | the query measured with planner, memory, JIT or parallelism settings changed for the session only, `apply` (session `SET` statements), `improved` |
| `verify_query_rewrite` | `sql`, `rewritten_sql` | equivalence verdict, before and after, `improved` |

`verify_*` roll everything back. `improved` is true only for a measured speedup (at least 1.1x
and at least 0.5 ms), with the new index actually used (indexes) and identical results
(rewrites; on the database's current data), and, for statistics, a changed plan. For a proven index or
statistics change, `apply` holds idempotent SQL.

## A typical loop

1. `get_top_queries`, then pick a query and substitute real values for `$1`, `$2`.
2. `get_query_plan`: look at the largest `self_ms`, at `estimate_off_by`, and at
   `rows_removed_by_filter` (a total across loops, like `actual_rows_total`).
3. `get_table_info` for each table in the plan: existing indexes and column statistics.
4. Propose one change, then `verify_index_suggestion`, `verify_statistics_suggestion`,
   `verify_setting_suggestion` or `verify_query_rewrite`.
5. If `improved` is false, read `note`, `error` and `uses_new_index`, then try something different.
   Do not resubmit the same idea.
6. Report only changes with `improved: true`.

## Notes

- Unsafe SQL, `$n` placeholders, a missing database configuration and connection failures come
  back as MCP errors whose text says why. A `verify_*` call that the database rejects (an unknown
  column, a bad value) comes back as a normal result with `improved: false` and an `error`, for
  example `UndefinedColumn: column "x" does not exist`, so the agent can correct itself.
- Index and rewrite proposals are validated before they run: identifier quoting, a safe
  partial-index predicate, one read-only statement.
- Starting the server with `pgsage-mcp` waits on stdio. Register it with your client rather than
  running it in a terminal.

# Security model

pgsage runs SQL that your AI agent sends it, against your database, and returns what it finds to
that agent. This page states what it defends against, how, and what it does not.

## Assumptions

- You point pgsage at a **dev or scratch database**, using a role with the **least privilege**
  that works (see below). The checks here are defense in depth, not a substitute for that.
- The agent calling pgsage can be prompt-injected (by a query it was pasted, by a file it read, by
  text inside your database), so pgsage treats every statement it receives as untrusted.
- Everything the database returns (names, comments, values, error text) can be malicious.

## Threats and mitigations

| Threat | Mitigation | Tests |
| --- | --- | --- |
| A write or DDL statement smuggled into a "SELECT" (`; COMMIT; DROP ...`, `DO`, `SELECT INTO`, `FOR UPDATE`) | Lexer-based gate (`ensure_read_only`) with a keyword and function deny-list that **fails closed** on anything it cannot lex unambiguously (unterminated quotes or comments, nested comments, line comments ended by a lone `\r`); and, independently of the lexer, **every statement is sent with the extended protocol (`prepare=True`), so PostgreSQL itself rejects a second statement** ("cannot insert multiple commands into a prepared statement") | `test_sql_gate.py` |
| Side effects through functions (`pg_sleep`, `nextval`, `set_config`, `dblink`, `lo_*`, advisory locks, user-defined functions) | Deny-list for known functions; **the database enforces read-only** (`SET TRANSACTION READ ONLY`) for every measurement, so a user-defined function that writes fails | `test_verify.py` |
| Identifier injection into DDL (`id"); COMMIT; DROP TABLE x; --`) | Every identifier goes through `quote_ident`; names validated; a bad name is a database error | `test_verify.py` |
| Partial-index predicate used as a side channel | One expression; no subqueries, comments, `;`, or deny-listed functions; validated before the proof runs | `test_verify.py` |
| Runaway queries and locks | `statement_timeout` 60s and `lock_timeout` 5s on every transaction: both proof phases, `get_plan`, `table_info`, `collect_tables`, `server_info` and `top_queries` | `test_verify.py`, `test_context.py` |
| Leaving changes behind | Everything rolls back; unique verify object names; tests assert nothing leaks | `test_verify.py` |
| MCP caller connecting to an arbitrary host or reusing ambient credentials | No tool takes a DSN; the server reads its own environment (`PGSAGE_DSN`, `PGSAGE_CONNECTION`) | `test_mcp.py`, `test_config.py` |
| A cloned repo's `pgsage.toml` running a program, leaking a secret, or sending credentials to a host it chooses | A project file may only name a local host, read `PGSAGE_*` variables, cannot set `service` or `options` or a password in a `dsn`, cannot redefine a user connection or change the default, and is searched only up to the repository root (never `$HOME`); `password_command` is honored only from the user config; a plaintext `password` key is rejected; labels never show expanded environment values; failing commands never echo their output | `test_config.py` |
| A query that hides a deny-listed function behind identifier tricks (`a$b$`, quoted names, `U&` escapes) or exploits the lexer's speed | A linear single-pass lexer with PostgreSQL's identifier rules; function names are checked with quoted identifiers unwrapped; `U&` escapes are refused; queries over 1,000,000 characters are refused; the statement and lock timeouts are re-applied before every measured statement, so even a query that resets them cannot run unbounded | `test_sql_gate.py`, `test_verify.py` |
| An agent flooding its own context with output | `inspect` caps the digest, tables, columns and indexes and says what it left out; `top` truncates statement text; the raw plan is opt-in | `test_context.py` |
| Tuning against a database that must not be touched | `protected = true` makes the `verify` commands and the MCP verify tools refuse the connection | `test_config.py`, `test_mcp.py` |
| Wrong "proof" | Equivalence check on names, types, precision and rows; unused index is not a win (partition children count); best of three; a 1.1x **and** 0.5 ms floor so microsecond noise cannot qualify; rewrites are measured interleaved; an identical rewrite, volatile functions and unordered `LIMIT` are never provable | `test_verify.py` |

## What the agent sees

pgsage has no model of its own, so it does not filter or rewrite what it returns: the agent sees
the plan (node types, costs, row counts, timings, buffers, filter and index conditions with their
literal values), table and column names, index definitions, column statistics (`n_distinct`,
`null_frac`, `correlation`) and database error text. `pgsage top` also returns query text from
`pg_stat_statements`. Row data is never read, but literals in your queries and in plan conditions
are visible to the agent, and through it to whatever model and provider your agent uses.

Treat all of it as **data from your database, not instructions**. A table named like an
instruction, a comment in a view, or an error message can try to steer the agent. pgsage cannot
stop a model from following such text; the skill tells the agent to treat tool output as data,
and the bounds below limit the damage if it does not.

If names or literals are sensitive, point your agent at a local model, and remember a hosted
provider may retain prompts according to its own policy.

## What bounds a fooled agent

A prompt-injected agent can call pgsage's tools with attacker-chosen arguments. It cannot:

- run a write or a second statement (the SQL gate and the extended protocol refuse it),
- escape the read-only measurement or leave a change behind (everything rolls back),
- choose another host or credentials (no tool takes a connection string),
- touch a `protected` connection with a proof,
- run a program (`password_command` is read only from your user config).

It can waste a proof, run an expensive `SELECT` (up to the 60 s timeout), or read names and
plan details from the database role pgsage connects as. That role is the real boundary.

## Residual risks

- **The agent sees names, literals and error text from your database.** See above.
- **A fooled agent can waste a proof or recommend a useless index.** It cannot run a write or
  reach another host.
- **Verification measures reads only.** A proven index may still slow writes or bloat the table.
- **Index builds lock the table** while a proof runs them (on your scratch database). The `apply`
  SQL uses `CREATE INDEX CONCURRENTLY`, which does not block writes but must run outside a
  transaction block (`psql -f` is fine). An interrupted concurrent build leaves an `INVALID`
  index to drop before re-running.
- **Statistics verification runs `ANALYZE`**, which updates `pg_class.reltuples` and `relpages` in
  place; a rollback does not undo that. The row-count estimates change to fresh, accurate values
  (harmless, but visible). The statistics objects and the per-column statistics do roll back.
- **`EXPLAIN ANALYZE` executes the query.** Expensive queries cost real resources up to the timeout.
- **The deny-list is not exhaustive.** Extensions can add side-effecting functions. The read-only
  transaction and a restricted role are the real controls.
- **A password in `--dsn` is visible in the process list.** Use a named connection.

## Least-privilege setup

`CREATE INDEX` needs table ownership, so proofs belong on a **scratch copy** of the data
(a restored snapshot), owned by a dedicated role. Never use an application or superuser role.

```sql
CREATE ROLE pgsage LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT pg_read_all_stats TO pgsage;           -- for pg_stat_statements
GRANT CONNECT ON DATABASE scratch TO pgsage;
```

`CREATE INDEX` requires owning the table, and owning the schema is not enough. Restore the scratch
copy as the `pgsage` role (`pg_restore --role=pgsage`, or `REASSIGN OWNED BY app_owner TO pgsage`
once restored), so it owns every table the proofs touch.

Run `pgsage top` against production statistics if you like (`pg_read_all_stats` only, and mark
that connection `protected = true`), then copy the interesting queries to the scratch database
for `inspect` and `verify`.

## Reporting

See [SECURITY.md](../SECURITY.md).

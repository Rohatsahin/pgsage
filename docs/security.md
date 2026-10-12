# Security model

pgsage executes SQL that originates from an LLM or from an MCP caller, and sends information about
your database to an LLM. This page states what it defends against, how, and what it does not.

## Assumptions

- You point pgsage at a **dev or scratch database**, using a role with the **least privilege**
  that works (see below). The checks here are defense in depth, not a substitute for that.
- The LLM and everything the database returns (names, comments, values) can be malicious.
- The MCP caller (an agent) can be prompt-injected.

## Threats and mitigations

| Threat | Mitigation | Tests |
| --- | --- | --- |
| A write or DDL statement smuggled into a "SELECT" (`; COMMIT; DROP ...`, `DO`, `SELECT INTO`, `FOR UPDATE`) | One statement only; lexer-based gate (`ensure_read_only`); keyword and function deny-list | `test_sql_gate.py` |
| Side effects through functions (`pg_sleep`, `nextval`, `set_config`, `dblink`, `lo_*`, advisory locks, user-defined functions) | Deny-list for known functions; **the database enforces read-only** (`SET TRANSACTION READ ONLY`) for every measurement, so a user-defined function that writes fails | `test_hardening_postgres.py` |
| Identifier injection into DDL (`id"); COMMIT; DROP TABLE x; --`) | Every identifier goes through `quote_ident`; names validated; a bad name is a database error | `test_hardening_postgres.py`, `test_migrations.py` |
| Partial-index predicate used as a side channel | One expression; no subqueries, comments, `;`, or deny-listed functions; re-validated after local restore | `test_injection.py`, `test_verify_postgres.py` |
| Runaway queries and locks | `statement_timeout` 60s and `lock_timeout` 5s on every transaction | `test_hardening_postgres.py` |
| Leaving changes behind | Everything rolls back; unique verify object names; tests assert nothing leaks | `test_verify_postgres.py` |
| MCP caller connecting to an arbitrary host or reusing ambient credentials | No tool takes a DSN; the server reads `PGSAGE_DSN` | `test_mcp_postgres.py` |
| Prompt injection through names, values, comments or error text | Non-plain identifiers aliased to tokens; literals redacted; comments stripped; control characters removed; length and count caps; random per-session boundary token; trust rules in the system prompt | `test_injection.py`, `test_injection_postgres.py`, `test_redaction.py` |
| A fooled model targeting something it should not | Allow-list: a proposal must name a table and columns in this query's tables; rewrites must be one read-only statement and prove equivalence; advisory `sql` is printed, never executed | `test_injection_postgres.py` |
| Wrong "proof" | Equivalence check on names, types and rows; unused index is not a win; best of three; 1.1x floor; volatile and unordered-`LIMIT` rewrites unprovable | `test_equivalent*`, `test_verify_postgres.py` |
| Eval clobbering real tables | Each case runs in its own `pgsage_eval_*` schema that is dropped afterwards | `test_hardening_postgres.py` |

## What goes to the LLM

Sent: the redacted plan (node types, costs, row counts, timings, buffers), a per-node digest, the
query with literals replaced by tokens, and per table the column names and types, index
definitions with literals masked, size, and per-column statistics (`n_distinct`, `null_frac`,
`correlation`). Table and column names are sent when they are plain identifiers.

Not sent: row data, literal values, comments, connection strings.

If names are sensitive, use a local model (`--provider openai --base-url http://localhost:11434/v1`).
Remember the provider may retain prompts according to its own policy.

## Residual risks

- **Plain-looking names still reach the model.** A 63-character name with no spaces can carry a
  short instruction. The system prompt tells the model to treat names as data, and the allow-list
  and proof bound the impact, but they cannot make a model immune.
- **A fooled model can waste a round trip or suggest a useless index.** It cannot run a write or
  reach another table.
- **Verification measures reads only.** A proven index may still slow writes or bloat the table.
- **Index builds lock the table** while they run. A migration written by `--apply-dir` uses plain
  `CREATE INDEX`; use `CONCURRENTLY` on production tables, outside a transaction.
- **`EXPLAIN ANALYZE` executes the query.** Expensive queries cost real resources up to the timeout.
- **The deny-list is not exhaustive.** Extensions can add side-effecting functions. The read-only
  transaction and a restricted role are the real controls.

## Least-privilege setup

`CREATE INDEX` needs table ownership, so verification belongs on a **scratch copy** of the data
(a restored snapshot), owned by a dedicated role. Never use an application or superuser role.

```sql
CREATE ROLE pgsage LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT pg_read_all_stats TO pgsage;           -- for pg_stat_statements
GRANT CONNECT ON DATABASE scratch TO pgsage;
-- then, in the scratch database:
ALTER SCHEMA public OWNER TO pgsage;         -- or ALTER TABLE ... OWNER TO pgsage per table
```

Run `pgsage top` against production statistics if you like (`pg_read_all_stats` only), then copy
the interesting queries to the scratch database for `tune`.

## Reporting

Please report suspected vulnerabilities privately. Include the input that triggers it; a failing
test is ideal.

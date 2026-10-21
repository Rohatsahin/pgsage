# Security policy

pgsage runs SQL that an AI agent sends it (through the command line or an MCP caller), so we treat reports about
the safety boundary seriously. The threat model and what is and is not protected is in
[docs/security.md](docs/security.md).

## Reporting a vulnerability

Please do not open a public issue for a security problem.

- Use GitHub's private vulnerability reporting on the repository (Security tab, "Report a
  vulnerability"), or email rohatsahin92@gmail.com.
- Include the input that triggers it. A failing test is ideal.

## What counts

- Making pgsage run a write or DDL statement that it did not generate itself
- Escaping the read-only measurement phases, or leaving a change behind after a proof
- Running a second statement, or an identifier that escapes its quoting
- Choosing the database or credentials from a tool call or command argument
- A project `pgsage.toml` running a program or revealing a secret
- Proving against a connection marked `protected`
- A proof reported as successful when the change is not faster or not equivalent

## What does not

- Behaviour that requires running pgsage with a superuser or an application role against a
  production database; use a least-privilege role on a scratch copy (see the security doc)
- An agent giving poor advice that pgsage then correctly refuses to call proven
- An agent following instructions hidden in names, comments or error text that your database returns (pgsage reports what the database says; see the security doc)

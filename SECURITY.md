# Security policy

pgsage runs SQL that originates from an LLM or an MCP caller, so we treat reports about
the safety boundary seriously. The threat model and what is and is not protected is in
[docs/security.md](docs/security.md).

## Reporting a vulnerability

Please do not open a public issue for a security problem.

- Use GitHub's private vulnerability reporting on the repository (Security tab, "Report a
  vulnerability"), or contact the maintainer directly.
- Include the input that triggers it. A failing test is ideal.

Maintainer note: enable private vulnerability reporting and add a contact address here before
publishing the repository.

## What counts

- Making pgsage run a write or DDL statement that it did not generate itself
- Escaping the read-only measurement phases, or leaving a change behind after a proof
- Executing or acting on a model-supplied name that is not part of the query being tuned
- A literal value, comment or hostile identifier reaching an LLM prompt unsanitized
- Choosing the database from an MCP tool call
- A proof reported as successful when the change is not faster or not equivalent

## What does not

- Behaviour that requires running pgsage with a superuser or an application role against a
  production database; use a least-privilege role on a scratch copy (see the security doc)
- An LLM giving poor advice that pgsage then correctly refuses to call proven

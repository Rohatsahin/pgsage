# Contributing

Thanks for helping. The short version:

1. `make install` then `make check`. Docker must be running (tests that need it skip cleanly
   without it, so `make check` still lints and type-checks).
2. Read [AGENTS.md](AGENTS.md): it lists the safety invariants and conventions, and applies to
   humans as well as agents.
3. Change behavior with a test first. Anything touching SQL execution, identifier handling,
   connection config or the proof needs a regression test that fails without the change.
4. Keep the PR small and say what you verified. If you changed a command, an MCP tool or a result
   field, change the skill (`src/pgsage/skill/SKILL.md`) and the docs in the same PR.

## Reporting a security issue

If you find a way to make pgsage run a write, escape the read-only phases, choose a database from
a tool call, or run a program from a project config, please report it privately
instead of opening a public issue. See [SECURITY.md](SECURITY.md) for how, and
[docs/security.md](docs/security.md) for the threat model.

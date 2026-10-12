# Contributing

Thanks for helping. The short version:

1. `make install` then `make check`. Docker must be running.
2. Read [AGENTS.md](AGENTS.md): it lists the safety invariants and conventions, and applies to
   humans as well as agents.
3. Change behavior with a test first. Anything touching SQL execution, identifier handling,
   redaction or the prompt needs a regression test that fails without the change.
4. Keep the PR small and say what you verified. If you changed the prompt, say which eval cases
   you ran and with which model.

## Reporting a security issue

If you find a way to make pgsage run a write, escape the read-only phases, leak a literal into a
prompt, or act on a model-supplied name that is not in the query, please report it privately
instead of opening a public issue. See [docs/security.md](docs/security.md) for the threat model.

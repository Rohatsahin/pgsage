## What changed

## How it was verified

- [ ] `make check` passes (lint, format, pyright strict, pytest)
- [ ] A regression test fails without the change (required for SQL execution, identifiers, connection config and the proof)
- [ ] The skill (`src/pgsage/skill/SKILL.md`) and docs are updated if a command, tool or result field changed
- [ ] No credentials, hostnames or local paths in the diff

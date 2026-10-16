#!/usr/bin/env bash
set -u

cd "$(dirname "$0")/.."

failed=0
results=()

step() {
  local label="$1"
  shift
  local started=$SECONDS
  printf '\n▶️  %s\n' "$label"
  if "$@"; then
    results+=("✅ $label ($((SECONDS - started))s)")
  else
    results+=("❌ $label ($((SECONDS - started))s)")
    failed=1
  fi
}

step "🧹 ruff check" uv run ruff check .
step "🎨 ruff format" uv run ruff format --check .
step "🔎 pyright (strict)" uv run pyright
step "🧪 pytest" uv run pytest -q -p no:cacheprovider

printf '\n──────── summary ────────\n'
printf '%s\n' "${results[@]}"
if [ "$failed" -eq 0 ]; then
  printf '\n🎉 all green\n'
else
  printf '\n💥 something needs attention\n'
fi
exit "$failed"

#!/usr/bin/env bash
# Optional one-command local E2E execution, including an isolated PG service.
# Run only from a trusted clone; never pass production credentials.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env.e2e ]]; then
  # This is a user-owned local shell configuration, not an untrusted upload.
  # shellcheck disable=SC1091
  source .env.e2e
fi
: "${E2E_POSTGRES_PASSWORD:?Set E2E_POSTGRES_PASSWORD in .env.e2e}"
: "${PERSONA_E2E_ADMIN_DSN:?Set PERSONA_E2E_ADMIN_DSN in .env.e2e}"

for arg in "$@"; do
  if [[ "$arg" == "--keep-db" ]]; then
    echo "--keep-db is incompatible with automatic Compose teardown." >&2
    echo "Use compose.e2e.yaml + scripts/persona_e2e.py directly to debug." >&2
    exit 2
  fi
done

# An invocation-unique Compose project prevents teardown of other projects.
project="persona-e2e-$(date +%s)-$$"
cleanup() {
  local result=$?
  trap - EXIT
  docker compose -f compose.e2e.yaml -p "$project" down -v --remove-orphans ||
    echo "WARNING: Compose teardown failed for $project" >&2
  exit "$result"
}
trap cleanup EXIT

docker compose -f compose.e2e.yaml -p "$project" up -d --wait
python scripts/persona_e2e.py "$@"

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
  case "$arg" in
    --keep-db|--publication-status-file|--publication-status-file=*)
      echo "$arg is incompatible with wrapper-managed cleanup/status." >&2
      echo "Use scripts/persona_e2e.py directly for manual lifecycle control." >&2
      exit 2
      ;;
  esac
done

# Reject invalid scenario content before starting a disposable Compose
# project. Reuse the actual Python validator as the single contract owner.
# A preflight failure has produced no report and no service to preserve.
python - "$@" <<'PY'
import argparse
import json
from pathlib import Path
import sys

from persona_agent.behavioral_evaluation import validate_scenario

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument(
    "--scenario", type=Path,
    default=Path("scenarios/mvp01e_conversation.json"),
)
args, _ = parser.parse_known_args(sys.argv[1:])
try:
    validate_scenario(json.loads(args.scenario.read_text(encoding="utf-8")))
except (OSError, ValueError) as exc:
    print(f"Invalid E2E scenario {args.scenario}: {exc}", file=sys.stderr)
    raise SystemExit(2)
PY

# The Python process alone knows whether ZIP publication succeeded.
# An invocation-private marker cannot be fabricated by a prior run.
status_dir="$(mktemp -d)"
status_file="$status_dir/publication.status"

# An invocation-unique Compose project prevents teardown of other projects.
project="persona-e2e-$(date +%s)-$BASHPID"
cleanup() {
  local result=$?
  trap - EXIT INT TERM
  if [[ -f "$status_file" && "$(<"$status_file")" == "PUBLISHED" ]]; then
    # A failing scenario still has useful evidence in its published ZIP.
    if ! docker compose -f compose.e2e.yaml -p "$project" down -v --remove-orphans; then
      echo "WARNING: Compose teardown failed for $project" >&2
      if [[ "$result" -eq 0 ]]; then result=2; fi
    fi
  else
    # No marker means the report failed or publication cannot be verified.
    # Do NOT remove the service or its anonymous PostgreSQL volume.
    echo "EVIDENCE NOT CONFIRMED: preserving Compose project $project" >&2
    echo "Inspect: docker compose -f compose.e2e.yaml -p $project ps" >&2
    echo "After recovering evidence, manually clean up with:" >&2
    echo "docker compose -f compose.e2e.yaml -p $project down -v" >&2
    if [[ "$result" -eq 0 ]]; then result=2; fi
  fi
  rm -rf -- "$status_dir"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker compose -f compose.e2e.yaml -p "$project" up -d --wait
python scripts/persona_e2e.py "$@" --publication-status-file "$status_file"

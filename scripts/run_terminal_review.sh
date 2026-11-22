#!/usr/bin/env bash
# Run a terminal review with its own disposable PostgreSQL Compose project.
# A missing publication marker or failed optional pg_dump preserves the server.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

mode="${1:-}"
if [[ "$mode" != human && "$mode" != goal-creation ]]; then
  echo "Usage: $0 {human|goal-creation} [--export-db] [--export-db-to FILE] [review options]" >&2
  exit 2
fi
shift

if [[ -f .env.e2e ]]; then
  # Operator-owned local configuration. Never forward its static admin DSN.
  # shellcheck disable=SC1091
  source .env.e2e
fi

export_db=false
dump_output=""
report_output=""
forward=()
while (( $# )); do
  case "$1" in
    --help|-h)
      cat <<'HELP'
Terminal review (disposable PostgreSQL)
  --export-db             Save a private .dump before removing PostgreSQL
  --export-db-to FILE     Save a private .dump at FILE (implies --export-db)
  --output FILE           Save the conversation review report at FILE
  --model MODEL           Choose the Ollama model (default: ACCEPTANCE_MODEL)
  --ollama-url URL        Choose the Ollama endpoint

The launcher creates its own PostgreSQL Compose project on a random loopback
port, tears it down only after confirmed evidence publication, and retains it
on publication/export errors. --keep-db and --admin-dsn are not supported by
this wrapper; use scripts/terminal_review_harness.py for manual DB control.
HELP
      exit 0
      ;;
    --export-db)
      export_db=true
      ;;
    --export-db-to)
      if (( $# < 2 )) || [[ -z "$2" ]]; then
        echo "--export-db-to requires a file path" >&2
        exit 2
      fi
      export_db=true; dump_output="$2"; shift
      ;;
    --export-db-to=*)
      export_db=true; dump_output="${1#*=}"
      ;;
    --output)
      if (( $# < 2 )) || [[ -z "$2" ]]; then
        echo "--output requires a file path" >&2
        exit 2
      fi
      report_output="$2"
      forward+=("$1" "$2")
      shift
      ;;
    --output=*)
      report_output="${1#*=}"
      forward+=("$1")
      ;;
    --model|--ollama-url)
      if (( $# < 2 )) || [[ -z "$2" || "$2" == --* ]]; then
        echo "$1 requires a nonempty value" >&2
        exit 2
      fi
      forward+=("$1" "$2")
      shift
      ;;
    --model=*|--ollama-url=*)
      if [[ -z "${1#*=}" ]]; then
        echo "$1 requires a nonempty value" >&2
        exit 2
      fi
      forward+=("$1")
      ;;
    *)
      # Explicit allowlist: do not pass abbreviations such as --admin-d or
      # --keep through to argparse, which otherwise accepts unique prefixes.
      echo "Unsupported terminal review argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

case "$mode" in
  human) extension="jsonl" ;;
  goal-creation) extension="json" ;;
esac
if [[ -z "$report_output" ]]; then
  report_output="artifacts/terminal-${mode}-$(date -u +%Y%m%dT%H%M%SZ)-$(python -c 'import secrets; print(secrets.token_hex(4))').${extension}"
  forward+=(--output "$report_output")
fi
if [[ "$report_output" != *."$extension" || -e "$report_output" || -L "$report_output" ]]; then
  echo "Report must be a new .${extension} file: $report_output" >&2
  exit 2
fi
if [[ "$export_db" == true ]]; then
  if [[ -z "$dump_output" ]]; then
    dump_output="${report_output%.*}.dump"
  fi
  if [[ -e "$dump_output" || -L "$dump_output" || "$dump_output" == "$report_output" ]]; then
    echo "Database dump must be a new, separate file: $dump_output" >&2
    exit 2
  fi
fi
command -v docker >/dev/null || { echo "Docker is required for terminal reviews" >&2; exit 2; }
if ! docker compose version >/dev/null; then
  echo "Docker Compose v2 is required for terminal reviews" >&2
  exit 2
fi

# Each invocation owns a fresh service and port; static .env.e2e admin DSN
# and E2E_POSTGRES_PORT are deliberately ignored.
if [[ -z "${E2E_POSTGRES_PASSWORD:-}" || "$E2E_POSTGRES_PASSWORD" == REPLACE_WITH_A_UNIQUE_LOCAL_PASSWORD ]]; then
  E2E_POSTGRES_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi
export E2E_POSTGRES_PASSWORD

status_dir="$(mktemp -d)"
status_file="$status_dir/publication.status"
project="persona-review-$(date +%s)-${BASHPID}-${RANDOM}"
compose=(docker compose -f compose.terminal-review.yaml -p "$project")
runner_started=false
dump_published=false

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  local remove=false
  if [[ "$runner_started" == false ]]; then
    remove=true
  elif [[ -f "$status_file" ]] && [[ "$(<"$status_file")" == PUBLISHED ]]; then
    if [[ "$export_db" == false || "$dump_published" == true ]]; then
      remove=true
    fi
  fi
  if [[ "$remove" == true ]]; then
    # Only our invocation-owned Compose project can be removed.
    if ! "${compose[@]}" down -v --remove-orphans; then
      echo "Teardown failed; inspect Compose project $project" >&2
      if (( rc == 0 )); then rc=2; fi
    fi
  else
    echo "Evidence/export unconfirmed; preserving Compose project: $project" >&2
    # Compose interpolates the password even for ps/exec/down. The real
    # ephemeral password is NOT needed to manage an existing container.
    # This dummy value satisfies interpolation without storing a secret.
    local recovery_compose="E2E_POSTGRES_PASSWORD=recovery-only docker compose -f compose.terminal-review.yaml -p $project"
    echo "Inspect: $recovery_compose ps" >&2
    echo "Recover with: $recovery_compose exec -T postgres psql -U postgres -d postgres" >&2
    echo "Clean up manually AFTER recovery: $recovery_compose down -v" >&2
    if (( rc == 0 )); then rc=2; fi
  fi
  rm -rf -- "$status_dir"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${compose[@]}" up -d --wait
port_address="$("${compose[@]}" port postgres 5432)"
review_port="${port_address##*:}"
if [[ ! "$review_port" =~ ^[0-9]+$ ]] || (( review_port < 1 || review_port > 65535 )); then
  echo "Could not determine the isolated PostgreSQL port: $port_address" >&2
  exit 2
fi
export REVIEW_POSTGRES_PORT="$review_port"
export PERSONA_E2E_ADMIN_DSN="$(python - <<'PY'
import os
from psycopg.conninfo import make_conninfo
print(make_conninfo(
    '', user='postgres', dbname='postgres', host='127.0.0.1',
    port=os.environ['REVIEW_POSTGRES_PORT'], password=os.environ['E2E_POSTGRES_PASSWORD'],
))
PY
)"

# The Python runner owns journal fsync, failure evidence and DB-drop gating.
# Retain the database until after pg_dump when export was requested.
internal=(--publication-status-file "$status_file")
if [[ "$export_db" == true ]]; then
  internal+=(--keep-db)
fi
runner_started=true
run_status=0
python scripts/terminal_review_harness.py "$mode" "${forward[@]}" "${internal[@]}" || run_status=$?

if [[ "$export_db" == true && -f "$status_file" && "$(<"$status_file")" == PUBLISHED ]]; then
  # Only one random persona_e2e_* DB can exist in this invocation-owned server.
  db_name="$("${compose[@]}" exec -T postgres psql -X -U postgres -d postgres -At \
    -c "SELECT datname FROM pg_database WHERE datname ~ '^persona_e2e_[0-9a-f]{16}$'")"
  if [[ ! "$db_name" =~ ^persona_e2e_[0-9a-f]{16}$ ]]; then
    echo "Expected exactly one isolated test database; retaining Compose project" >&2
    exit 2
  fi
  mkdir -p -- "$(dirname -- "$dump_output")"
  temporary="$(mktemp "${dump_output}.partial.XXXXXXXX")"
  if ! "${compose[@]}" exec -T postgres pg_dump -U postgres -d "$db_name" \
        --format=custom --no-owner --no-acl > "$temporary"; then
    rm -f -- "$temporary"
    echo "PostgreSQL dump failed; retaining Compose project $project" >&2
    exit 2
  fi
  # A private temp file, durable file + directory fsync, and no-clobber link.
  # Never publish a partial dump or overwrite a prior run's evidence.
  python - "$temporary" <<'PY'
import os, sys
with open(sys.argv[1], 'rb') as stream:
    os.fsync(stream.fileno())
PY
  if ! ln -- "$temporary" "$dump_output"; then
    rm -f -- "$temporary"
    echo "Could not publish database dump; retaining Compose project" >&2
    exit 2
  fi
  python - "$(dirname -- "$dump_output")" <<'PY'
import os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
PY
  rm -f -- "$temporary"
  dump_published=true
  echo "Private PostgreSQL snapshot: $dump_output"
fi
exit "$run_status"

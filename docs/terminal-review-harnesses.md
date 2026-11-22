# Terminal review harnesses (v2.5.1)

Both entry points now start **their own disposable PostgreSQL 16 container**
with a random loopback-only host port, apply migrations to a new test database,
run the real Ollama-backed review, then remove **only the invocation-owned**
Docker Compose project and volume after successful evidence publication.
These are local developer tools, not a production authentication endpoint.

Prerequisites: Docker with Compose v2, Python dependencies installed with
`python -m pip install -e '.[test]'`, and a running Ollama server with
the requested model (e.g. `ollama pull qwen2.5:7b`). No manual PostgreSQL
startup, .env.e2e, fixed port or stored database credentials are required.
If supplied, .env.e2e is read for Ollama/model configuration but its fixed
database connection string and port are ignored.

## Human review

    scripts/start-terminal-review-human --model qwen2.5:7b
    scripts/start-terminal-review-human --export-db
    scripts/start-terminal-review-human --output artifacts/review.jsonl \
      --export-db-to artifacts/review.dump

Talk normally. Use /session NAME for another conversation on the same persona,
 /restart to recreate the host, /state for scoped counts, /rate 0|1|2 NOTE to
record manual feedback, and /quit to finish.

## Repeatable goal-creation injection

    scripts/start-terminal-injection-test-goal-creation --model qwen2.5:7b
    scripts/start-terminal-injection-test-goal-creation --export-db

The injection checks tentative goal signals, correction, explicit approval,
idempotent Goal-owner admission, and new-session persistence. It does NOT
claim that a model automatically converts natural dialogue into approved goals.
Qualitative responses remain pending independent human review.

## Evidence, optional export and recovery

The private human journal (.jsonl) or goal report (.json) is written under
artifacts/ by default. Export is optional: --export-db creates a private
PostgreSQL custom-format dump (.dump) next to the report, using pg_dump
**before** Docker teardown. --export-db-to FILE chooses the dump destination.
The dump includes conversation text and persistent persona state; handle it
as sensitive and restore only to a separate disposable database with pg_restore.

On successful journal/report publication (and successful dump if requested),
the wrapper stops and removes its own container and volume. If publication,
journal close, dump, or dump publication fails, it preserves that container
and volume, printing its unique Compose project and recovery commands.
A failing goal assertion can still produce a published failure report and exit
nonzero; this does not block safe teardown after evidence has been retained.
Avoid deleting a preserved volume before inspecting the failure.

The direct Python script still supports --admin-dsn and --keep-db for manual
development; wrapper-managed invocations deliberately reject those options
because they would conflict with the container teardown contract.

## Tests

    pytest -q tests/test_terminal_review_wrapper.py
    pytest -q tests/test_terminal_review_harness.py
    bash -n scripts/run_terminal_review.sh \
      scripts/start-terminal-review-human \
      scripts/start-terminal-injection-test-goal-creation

The wrapper tests simulate Docker lifecycle and export; they do not substitute
for a real Docker/PostgreSQL/Ollama integration run. The existing
scripts/persona_e2e.py and scripts/review_natural_dialogue.py remain the
full scenario and blinded human-review workflows.

## Launcher argument and recovery safety

Only the exact wrapper options `--model`, `--ollama-url`, `--output`,
`--export-db` and `--export-db-to` are accepted (plus `--help`).
Database-control arguments and their abbreviations, such as `--admin-d`
and `--keep`, fail **before Docker starts**. The Python runner also disables
long-option abbreviation.

On failure, printed commands include `E2E_POSTGRES_PASSWORD=recovery-only`.
This is an **interpolation placeholder**, not the ephemeral PostgreSQL
password: Compose requires a nonempty value even for `ps`, `exec`, and
`down`. It makes recovery possible in a new shell without saving the actual
password. Use only the printed inspection/recovery/cleanup commands for the
already-running project; the placeholder cannot restart a fresh database.
The `exec ... psql` command uses the container's local Unix socket.

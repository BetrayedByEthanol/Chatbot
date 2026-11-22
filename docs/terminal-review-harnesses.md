# Local terminal review harnesses (v2.5.1)

These entry points reuse the existing `BehavioralAcceptanceHarness`,
`LocalAcceptanceSession`, PostgreSQL migrations, Ollama gateway and Goal owner.
They are **local developer tests**, not a public chat server or an automatic
verdict on natural dialogue. No new scheduler, permanent identity, or tool
permission is introduced.

## Prerequisites

```bash
python -m pip install -e '.[test]'
cp .env.e2e.example .env.e2e  # edit to set a strong local password
# Only use the repository's disposable Postgres server.
docker compose -f compose.e2e.yaml -p persona-terminal up -d --wait
ollama pull qwen2.5:7b
# Ensure `ollama serve` is running separately.
```

The commands below load `.env.e2e` from the repository root, use
`PERSONA_E2E_ADMIN_DSN` pointing to the `postgres` **maintenance database**
on this isolated server, and create a fresh random `persona_e2e_*` database.
Each run applies all migrations and drops **only its own** database after
successfully publishing its local report. A failed report publication retains
the test DB for forensic recovery. `--keep-db` explicitly retains it.

## 1. Human-driven live terminal conversation

```bash
scripts/start-terminal-review-human --model qwen2.5:7b
```

Talk normally with the persona. Useful commands:

- `/session revisit` starts a new conversation on the **same** persona instance.
- `/restart` rebuilds the host/session without replacing persistent state.
- `/state` prints scoped counts only (no automatic interpretation of them).
- `/rate 0 response invented a detail` records a human rating (0 fail, 1 partial, 2 good).
- `/quit` ends the session and releases the isolated DB.

The private, permission-restricted JSONL evidence file is written under
`artifacts/` and flushed on each turn. It includes **the user's exact input,
the model's exact reply, and any human rating**; do not enter secrets or
sensitive real-person data. Data is never posted to public CI or GitHub by
these scripts. A rating is the reviewer's observation, not an automatic release
certification. This mode currently exercises model-routed text conversation;
it does not run full-duplex voice or general autonomous tools.

## 2. Repeatable goal-creation injection

```bash
scripts/start-terminal-injection-test-goal-creation --model qwen2.5:7b
```

The fixture injects ordinary user messages about a walking goal, a frequency
correction, a topic switch, and a recall request. It then verifies:

1. An objective signal is source-linked, but no approved Goal exists yet.
2. The Goal owner rejects creation **without** a separate approval turn.
3. The script injects the domain's exact, authenticated approval challenge.
4. `create_goal` commits one approved root via the production Goal domain.
5. Retrying the same command is idempotent, and a new conversation after
   host restart still reads one approved Goal.

The script **does not** pretend that the persona automatically converted
natural language into a user-approved Goal. It is explicitly testing two
separate boundaries: tentative conversational interpretation, and safe
Goal-domain approval. The replies are captured for later qualitative review,
which remains `PENDING_HUMAN_REVIEW` regardless of passing database checks.

An isolated private JSON report is written to `artifacts/`. A nonzero exit
status means at least one contract check or runtime operation failed.

## Verification

```bash
pytest -q tests/test_terminal_review_harness.py  # requires TEST_DATABASE_URL
bash -n scripts/start-terminal-review-human \
  scripts/start-terminal-injection-test-goal-creation
```

Use the existing `scripts/persona_e2e.py` and
`scripts/review_natural_dialogue.py` pipeline for larger versioned scripted
natural-dialogue scenarios and two independent human reviewer scorecards.
These terminal entry points complement rather than replace that workflow.

When finished with the locally managed test Postgres service:

```bash
docker compose -f compose.e2e.yaml -p persona-terminal down -v
```

Do not use `down -v` for any other Compose project or shared database.

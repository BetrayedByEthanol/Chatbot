"""No-Docker lifecycle regression for the terminal review Compose launcher.

The fake commands verify orchestration, not the real PostgreSQL integration;
real migrations and goal admission remain covered by the existing E2E suite.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_review(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "run_terminal_review.sh", repo / "scripts")
    shutil.copy(ROOT / "compose.terminal-review.yaml", repo)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text("""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$REVIEW_DOCKER_LOG"
if [[ "$*" == 'compose version' ]]; then exit 0; fi
case " $* " in
  *' port postgres 5432 '*) echo '127.0.0.1:55444' ;;
  *' exec -T postgres psql '*)
    if [[ "${REVIEW_BAD_DB:-}" == 1 ]]; then echo 'unexpected_db';
    else echo 'persona_e2e_0123456789abcdef'; fi ;;
  *' exec -T postgres pg_dump '*)
    if [[ "${REVIEW_FAIL_DUMP:-}" == 1 ]]; then exit 1; fi
    printf 'PGDMP synthetic test content' ;;
esac
""", encoding="utf-8")
    docker.chmod(0o755)
    python = fake_bin / "python"
    python.write_text("""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == 'scripts/terminal_review_harness.py' ]]; then
  report=''; marker=''
  while (( $# )); do
    case "$1" in
      --output) report="$2"; shift ;;
      --publication-status-file) marker="$2"; shift ;;
    esac
    shift
  done
  [[ -n "$report" && -n "$marker" ]] || exit 5
  mkdir -p "$(dirname -- "$report")"
  printf '%s\\n' '{"result":"private review"}' > "$report"
  if [[ "${REVIEW_NO_MARKER:-}" != 1 ]]; then
    printf 'PUBLISHED\\n' > "$marker"
  fi
  exit "${REVIEW_RUN_STATUS:-0}"
fi
if [[ "$1" == '-c' ]]; then printf 'abcd1234\\n'; exit 0; fi
if [[ "$1" == '-' && $# == 1 ]]; then
  printf 'dbname=postgres host=127.0.0.1 port=55444 user=postgres password=test\\n'
fi
""", encoding="utf-8")
    python.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
        "REVIEW_DOCKER_LOG": str(docker_log),
        "E2E_POSTGRES_PASSWORD": "local-fake-credential",
    })

    def run(mode="human", *args, **overrides):
        custom_env = {**env, **{k: str(v) for k, v in overrides.items()}}
        return subprocess.run(
            ["bash", "scripts/run_terminal_review.sh", mode, *map(str, args)],
            cwd=repo, env=custom_env, capture_output=True, text=True,
            timeout=20, check=False,
        )

    return run, tmp_path, docker_log


def test_automatic_container_teardown_after_published_human_review(fake_review):
    run, tmp, log = fake_review
    report = tmp / "human.jsonl"
    result = run("human", "--output", report)
    assert result.returncode == 0, result.stderr
    assert report.is_file()
    events = log.read_text().splitlines()
    assert any(" up -d --wait" in x for x in events)
    assert any(" port postgres 5432" in x for x in events)
    assert any(" down -v --remove-orphans" in x for x in events)
    assert not any(" pg_dump " in x for x in events)


def test_optional_dump_is_published_before_container_teardown(fake_review):
    run, tmp, log = fake_review
    report = tmp / "human.jsonl"
    dump = tmp / "human.dump"
    result = run("human", "--output", report, "--export-db-to", dump)
    assert result.returncode == 0, result.stderr
    assert dump.read_bytes().startswith(b"PGDMP")
    assert dump.stat().st_mode & 0o777 == 0o600
    events = log.read_text().splitlines()
    assert next(i for i, x in enumerate(events) if " pg_dump " in x) < next(
        i for i, x in enumerate(events) if " down -v" in x
    )


def test_exists_but_unpublished_report_preserves_compose(fake_review):
    run, tmp, log = fake_review
    report = tmp / "partial.jsonl"
    result = run("human", "--output", report, REVIEW_NO_MARKER=1, REVIEW_RUN_STATUS=1)
    assert result.returncode == 1
    assert report.exists()
    assert "preserving Compose project" in result.stderr
    assert not any(" down -v" in x for x in log.read_text().splitlines())


def test_dump_failure_preserves_container_without_partial_publication(fake_review):
    run, tmp, log = fake_review
    report = tmp / "goal.json"
    dump = tmp / "goal.dump"
    result = run("goal-creation", "--output", report, "--export-db-to", dump,
                 REVIEW_FAIL_DUMP=1)
    assert result.returncode == 2
    assert report.exists() and not dump.exists()
    assert "preserving Compose project" in result.stderr
    assert not any(" down -v" in x for x in log.read_text().splitlines())


def test_no_clobber_preflight_does_not_start_compose(fake_review):
    run, tmp, log = fake_review
    output = tmp / "existing.json"
    output.write_text("original")
    result = run("goal-creation", "--output", output)
    assert result.returncode == 2
    assert output.read_text() == "original"
    assert not log.exists()


def test_invalid_isolated_database_name_preserves_compose(fake_review):
    run, tmp, log = fake_review
    result = run("goal-creation", "--output", tmp / "goal.json", "--export-db",
                 REVIEW_BAD_DB=1)
    assert result.returncode == 2
    assert "preserving Compose project" in result.stderr
    assert not any(" down -v" in x for x in log.read_text().splitlines())

"""E2E Compose shell wrapper publication/teardown status regression.

No Docker daemon or PostgreSQL is needed: subprocesses are replaced with
restricted dummy executables. Preserve the real Bash cleanup implementation.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_e2e(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "run_persona_e2e.sh", repo / "scripts")
    shutil.copy(ROOT / "compose.e2e.yaml", repo / "compose.e2e.yaml")
    (repo / ".env.e2e").write_text(
        "export E2E_POSTGRES_PASSWORD=local-test-only\n"
        "export PERSONA_E2E_ADMIN_DSN=postgresql://local-test/postgres\n"
    )
    bins = tmp_path / "bin"
    bins.mkdir()
    logs = tmp_path / "docker.log"
    docker = bins / "docker"
    docker.write_text("""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
if [[ "$*" == *" down -v "* && "${FAIL_DOWN:-}" == 1 ]]; then exit 22; fi
""", encoding="utf-8")
    docker.chmod(0o755)
    python = bins / "python"
    python.write_text("""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "-" ]]; then cat >/dev/null; exit 0; fi
if [[ "$1" != "scripts/persona_e2e.py" ]]; then exit 10; fi
while (( $# )); do
  if [[ "$1" == "--publication-status-file" ]]; then
    marker="$2"
    if [[ "${PUBLISH_MARKER:-}" == 1 ]]; then printf 'PUBLISHED\\n' > "$marker"; fi
    break
  fi
  shift
done
exit "${FAKE_RUNNER_EXIT:-0}"
""", encoding="utf-8")
    python.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bins) + os.pathsep + env.get("PATH", "")
    env["FAKE_DOCKER_LOG"] = str(logs)

    def run(*, published: bool, runner_exit: int, fail_down: bool = False):
        invocation_env = {**env, "PUBLISH_MARKER": str(int(published)),
                          "FAKE_RUNNER_EXIT": str(runner_exit),
                          "FAIL_DOWN": str(int(fail_down))}
        return subprocess.run(
            ["bash", "scripts/run_persona_e2e.sh"],
            cwd=repo, env=invocation_env, text=True, capture_output=True,
            check=False, timeout=15,
        )

    return run, logs


@pytest.mark.parametrize("runner_exit", [0, 1])
def test_unconfirmed_publication_returns_two_and_preserves_compose(
    fake_e2e, runner_exit,
):
    run, log = fake_e2e
    result = run(published=False, runner_exit=runner_exit)
    assert result.returncode == 2, result.stderr
    assert "EVIDENCE NOT CONFIRMED" in result.stderr
    actions = log.read_text()
    assert " up -d --wait" in actions
    assert " down -v" not in actions


def test_published_contract_failure_returns_one_and_tears_down(fake_e2e):
    run, log = fake_e2e
    result = run(published=True, runner_exit=1)
    assert result.returncode == 1, result.stderr
    assert " down -v --remove-orphans" in log.read_text()


def test_published_success_returns_zero_and_tears_down(fake_e2e):
    run, log = fake_e2e
    result = run(published=True, runner_exit=0)
    assert result.returncode == 0, result.stderr
    assert " down -v --remove-orphans" in log.read_text()


@pytest.mark.parametrize("runner_exit", [0, 1])
def test_failed_teardown_returns_two_regardless_of_runner_status(
    fake_e2e, runner_exit,
):
    run, log = fake_e2e
    result = run(published=True, runner_exit=runner_exit, fail_down=True)
    assert result.returncode == 2, result.stderr
    assert "WARNING: Compose teardown failed" in result.stderr
    assert " down -v --remove-orphans" in log.read_text()

"""Terminal review harness regression using the real PostgreSQL domain APIs."""
from pathlib import Path
import sys

import pytest

pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from terminal_review_harness import PrivateJournal, _save_private, goal_injection
from persona_agent.conversation_contracts import ModelReply


class FakeGateway:
    model = "test-review-only"

    def generate(self, messages):
        return ModelReply(model_id=self.model, text="I can discuss that with you.")


def test_goal_injection_requires_real_user_approval_and_replays_safely(dsn):
    result = goal_injection(dsn, FakeGateway())
    assert result["passed"]
    assert result["manual_review"] == "PENDING_HUMAN_REVIEW"
    assert len(result["transcript"]) == 6
    assert all(item["pass"] for item in result["checks"])
    assert {x["check"] for x in result["checks"]} >= {
        "no_implicit_goal_before_approval", "objective_signal_source_linked",
        "owner_rejects_missing_confirmation", "negative_attempt_commits_no_goal",
        "duplicate_command_replays", "exactly_one_approved_root",
        "goal_survives_new_session",
    }


def test_private_reports_never_overwrite_or_change_world_permissions(tmp_path):
    path = tmp_path / "report.json"
    _save_private(path, {"status": "PENDING_HUMAN_REVIEW"})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        _save_private(path, {"status": "FAKE_PASS"})


def test_human_journal_is_private_and_durable(tmp_path):
    path = tmp_path / "session.jsonl"
    journal = PrivateJournal(path)
    journal.append({"type": "human_rating", "score": 1, "turn_id": "abc"})
    journal.close()
    assert path.stat().st_mode & 0o777 == 0o600
    assert '"score": 1' in path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        PrivateJournal(path)

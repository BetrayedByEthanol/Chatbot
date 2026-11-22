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


def test_goal_injection_never_reconfigures_existing_tool_pool(dsn, monkeypatch):
    """A model-only test must not shrink capacity reserved by prior tests."""
    import persona_agent.acceptance_host as host

    def forbidden_reconfiguration(*args, **kwargs):
        raise AssertionError("model-only goal injection reconfigured tool pool")

    monkeypatch.setattr(host, "configure_resource_pool", forbidden_reconfiguration)
    result = goal_injection(dsn, FakeGateway())
    assert result["passed"]


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


def test_human_journal_fsync_failure_preserves_isolated_database(tmp_path, monkeypatch):
    """A present-but-partial journal must never trigger database teardown."""
    import terminal_review_harness as terminal

    retained = []
    monkeypatch.setattr(terminal, "_prepare", lambda dsn: ("persona_e2e_test", "db"))
    monkeypatch.setattr(terminal, "OllamaChatGateway", lambda **kwargs: FakeGateway())
    monkeypatch.setattr(
        terminal, "drop_isolated_database", lambda *args: retained.append(args),
    )
    monkeypatch.setattr(
        terminal, "human_session",
        lambda dsn, gateway, journal: journal.append({"type": "start"}),
    )

    def fail_fsync(fd):
        raise OSError("simulated disk sync failure")

    monkeypatch.setattr(terminal.os, "fsync", fail_fsync)
    report = tmp_path / "partial.jsonl"
    code = terminal.main([
        "human", "--admin-dsn", "maintenance", "--output", str(report),
    ])
    assert code == 1
    assert report.exists()  # The original bug treated this as success.
    assert retained == []  # The authoritative recovery source must survive.


def test_human_journal_success_drops_isolated_database(tmp_path, monkeypatch):
    import terminal_review_harness as terminal

    dropped = []
    monkeypatch.setattr(terminal, "_prepare", lambda dsn: ("persona_e2e_test", "db"))
    monkeypatch.setattr(terminal, "OllamaChatGateway", lambda **kwargs: FakeGateway())
    monkeypatch.setattr(
        terminal, "drop_isolated_database", lambda *args: dropped.append(args),
    )

    def complete_session(dsn, gateway, journal):
        journal.append({"type": "start"})
        journal.append({"type": "end"})

    monkeypatch.setattr(terminal, "human_session", complete_session)
    report = tmp_path / "complete.jsonl"
    code = terminal.main([
        "human", "--admin-dsn", "maintenance", "--output", str(report),
    ])
    assert code == 0
    assert dropped == [("maintenance", "persona_e2e_test")]
    assert '"type": "end"' in report.read_text()


def test_human_journal_close_failure_preserves_isolated_database(tmp_path, monkeypatch):
    import terminal_review_harness as terminal

    dropped = []
    monkeypatch.setattr(terminal, "_prepare", lambda dsn: ("persona_e2e_test", "db"))
    monkeypatch.setattr(terminal, "OllamaChatGateway", lambda **kwargs: FakeGateway())
    monkeypatch.setattr(
        terminal, "drop_isolated_database", lambda *args: dropped.append(args),
    )
    monkeypatch.setattr(
        terminal, "human_session",
        lambda dsn, gateway, journal: journal.append({"type": "end"}),
    )
    original_close = terminal.PrivateJournal.close

    def fail_close(journal):
        original_close(journal)
        raise OSError("simulated close failure")

    monkeypatch.setattr(terminal.PrivateJournal, "close", fail_close)
    report = tmp_path / "close-error.jsonl"
    code = terminal.main([
        "human", "--admin-dsn", "maintenance", "--output", str(report),
    ])
    assert code == 1
    assert report.exists()
    assert dropped == []


def test_runner_publishes_marker_only_after_complete_journal(tmp_path, monkeypatch):
    import terminal_review_harness as terminal

    dropped = []
    monkeypatch.setattr(terminal, "_prepare", lambda dsn: ("persona_e2e_test", "db"))
    monkeypatch.setattr(terminal, "OllamaChatGateway", lambda **kwargs: FakeGateway())
    monkeypatch.setattr(terminal, "drop_isolated_database", lambda *args: dropped.append(args))
    monkeypatch.setattr(
        terminal, "human_session",
        lambda dsn, gateway, journal: journal.append({"type": "end"}),
    )
    marker = tmp_path / "published.marker"
    report = tmp_path / "complete.jsonl"
    code = terminal.main([
        "human", "--admin-dsn", "maintenance", "--output", str(report),
        "--publication-status-file", str(marker),
    ])
    assert code == 0
    assert marker.read_text() == "PUBLISHED\n"
    assert '"type": "end"' in report.read_text()
    assert dropped == [("maintenance", "persona_e2e_test")]


@pytest.mark.parametrize("mode", ["human", "goal-creation"])
def test_marker_failure_retains_db_even_when_report_exists(
    tmp_path, monkeypatch, mode,
):
    import terminal_review_harness as terminal

    dropped = []
    monkeypatch.setattr(terminal, "_prepare", lambda dsn: ("persona_e2e_test", "db"))
    monkeypatch.setattr(terminal, "OllamaChatGateway", lambda **kwargs: FakeGateway())
    monkeypatch.setattr(terminal, "drop_isolated_database", lambda *args: dropped.append(args))
    monkeypatch.setattr(
        terminal, "human_session",
        lambda dsn, gateway, journal: journal.append({"type": "end"}),
    )
    monkeypatch.setattr(
        terminal, "goal_injection",
        lambda dsn, gateway, report: {"checks": [], "passed": True},
    )

    def failed_marker(path):
        raise OSError("simulated publication marker error")

    monkeypatch.setattr(terminal, "publish_status_file", failed_marker)
    report = tmp_path / ("complete.jsonl" if mode == "human" else "complete.json")
    marker = tmp_path / "missing.marker"
    code = terminal.main([
        mode, "--admin-dsn", "maintenance", "--output", str(report),
        "--publication-status-file", str(marker),
    ])
    assert code == 1
    assert report.exists()
    assert not marker.exists()
    assert dropped == []


def test_keep_db_for_wrapper_export_after_publication(tmp_path, monkeypatch):
    import terminal_review_harness as terminal

    dropped = []
    monkeypatch.setattr(terminal, "_prepare", lambda dsn: ("persona_e2e_test", "db"))
    monkeypatch.setattr(terminal, "OllamaChatGateway", lambda **kwargs: FakeGateway())
    monkeypatch.setattr(terminal, "drop_isolated_database", lambda *args: dropped.append(args))
    monkeypatch.setattr(
        terminal, "goal_injection",
        lambda dsn, gateway, report: {"checks": [], "passed": True},
    )
    marker = tmp_path / "published.marker"
    code = terminal.main([
        "goal-creation", "--admin-dsn", "maintenance",
        "--output", str(tmp_path / "goal.json"),
        "--publication-status-file", str(marker),
        "--keep-db",
    ])
    assert code == 0
    assert marker.read_text() == "PUBLISHED\n"
    assert dropped == []


@pytest.mark.parametrize("abbreviation,argument", [
    ("--admin-d", "postgresql://localhost/external"),
    ("--keep", None),
    ("--publication-status", "external.status"),
])
def test_direct_runner_rejects_abbreviated_control_options(abbreviation, argument):
    """Defense in depth: even direct Python calls cannot abbreviate DB controls."""
    from terminal_review_harness import main

    argv = ["human", "--admin-dsn", "maintenance", abbreviation]
    if argument is not None:
        argv.append(argument)
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2

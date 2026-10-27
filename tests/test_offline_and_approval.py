"""Focused tests for provider removal, offline deterministic analysis, and Gmail human approval."""

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from bug_agent.ai_analyzer import MockAIProvider
from bug_agent.approval_manager import ApprovalManager
from bug_agent.cli import main as cli_main
from bug_agent.config import AppConfig
from bug_agent.gmail_client import GmailAuthError, GmailClient, InboundEmail, MockGmailClient
from bug_agent.models import Task, TaskStatus
from bug_agent.task_store import TaskStore


def test_codex_provider_removed():
    """Verify OpenAI Codex adapter module has been completely removed."""
    with pytest.raises((ModuleNotFoundError, ImportError)):
        importlib.import_module("bug_agent.codex_adapter")


def test_cli_rejects_codex_provider():
    """Verify CLI rejects --ai-provider codex and only allows offline mock provider."""
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--ai-provider", "codex"])
    assert exc_info.value.code != 0


def test_offline_analysis_is_deterministic_and_no_paid_service():
    """Verify defect analysis runs offline, deterministically, with no network or API keys."""
    provider = MockAIProvider()
    assert provider.name == "mock-provider"
    assert provider.is_available() is True

    # 1. Defect with bug keywords
    res1 = provider.analyze_defect(
        task_id="TASK-offline-1",
        repository="owner/repo",
        issue_number=10,
        issue_title="Unexpected crash on null input",
        issue_body="The service crashes with a NullPointerException when payload is empty.",
        file_contexts={"service.py": "def handle(payload):\n    return payload.get('val')\n"},
    )
    assert res1.is_bug is True
    assert res1.bug_classification == "SUSPECTED_BUG"
    assert res1.confidence_level == "HIGH"
    assert res1.is_fix_tested is False
    assert len(res1.relevant_files) == 1
    assert res1.relevant_files[0].path == "service.py"

    # 2. Defect without bug keywords
    res2 = provider.analyze_defect(
        task_id="TASK-offline-2",
        repository="owner/repo",
        issue_number=11,
        issue_title="Update documentation styling",
        issue_body="Please change the navbar color to dark blue.",
    )
    assert res2.is_bug is False
    assert res2.bug_classification == "NEEDS_MORE_INFO"
    assert res2.is_fix_tested is False

    # 3. Deterministic patch proposal
    patches = provider.propose_patch(
        task_id="TASK-offline-1",
        repository="owner/repo",
        analysis=res1,
        file_contents={"service.py": "def handle(payload):\n    return payload.get('val')\n"},
    )
    assert "service.py" in patches
    assert "Fix applied for TASK-offline-1" in patches["service.py"]


def test_gmail_used_only_for_human_approval(tmp_path: Path):
    """Verify Gmail is used exclusively for human approval routing and decision parsing."""
    db_file = tmp_path / "offline_approval.db"
    store = TaskStore(db_path=str(db_file))
    provider = MockAIProvider()

    task, _ = store.record_discovered_task("pytorch/ignite", 200, "Bug in optimizer", "http://issue/200")
    analysis = provider.analyze_defect(
        task_id=task.task_id,
        repository=task.repository,
        issue_number=task.issue_number,
        issue_title=task.issue_title,
        issue_body="Bug in optimizer causing crash",
    )
    store.update_task_status(
        task.task_id,
        TaskStatus.ANALYZED,
        extra_fields={"analysis_result": json.dumps(analysis.to_dict())},
    )
    analyzed_task = store.get_task(task.task_id)

    mock_gmail = MockGmailClient(approver_email="human_reviewer@example.com")
    config = AppConfig(repositories=["pytorch/ignite"], approver_email="human_reviewer@example.com")
    mgr = ApprovalManager(task_store=store, gmail_client=mock_gmail, config=config)

    # 1. Request human approval
    req = mgr.request_approval_for_task(analyzed_task)
    assert req is not None
    assert len(mock_gmail.sent_messages) == 1
    sent_msg = mock_gmail.sent_messages[0]
    assert sent_msg["to"] == "human_reviewer@example.com"
    assert "APPROVAL REQUIRED" in sent_msg["subject"]
    assert "APPROVE TASK-pytorch-ignite-200" in sent_msg["body_text"]
    assert "REJECT TASK-pytorch-ignite-200" in sent_msg["body_text"]

    # 2. Human approval response
    inbound_approve = InboundEmail(
        message_id="msg-human-approve",
        thread_id="thread-human-1",
        sender="human_reviewer@example.com",
        subject="Re: Approval Request",
        body=f"APPROVE {task.task_id}",
        date="2026-10-27T12:00:00Z",
    )
    success, msg = mgr.process_incoming_decision(inbound_approve)
    assert success is True
    assert "APPROVED" in msg
    updated_task = store.get_task(task.task_id)
    assert updated_task.status == TaskStatus.APPROVED

    # 3. Unauthorized sender is strictly rejected
    inbound_unauth = InboundEmail(
        message_id="msg-unauth",
        thread_id="thread-human-2",
        sender="attacker@evil.com",
        subject="Re: Approval",
        body=f"APPROVE {task.task_id}",
        date="2026-10-27T12:05:00Z",
    )
    success_unauth, reason = mgr.process_incoming_decision(inbound_unauth)
    assert success_unauth is False
    assert "Unauthorized sender" in reason


def test_gmail_oauth_fails_without_env_secrets(monkeypatch):
    """Verify production Gmail client requires environment secrets and fails closed."""
    monkeypatch.delenv("GMAIL_TOKEN_JSON", raising=False)
    monkeypatch.delenv("GMAIL_CREDENTIALS_JSON", raising=False)

    client = GmailClient()
    with pytest.raises(GmailAuthError, match="GMAIL_TOKEN_JSON is required"):
        client._get_service()


def test_repository_scope_enforcement_in_scanner(monkeypatch):
    """Verify issue scanner rejects out-of-scope repositories before fetching issues."""
    from bug_agent.issue_scanner import IssueScanner
    from bug_agent.github_client import GitHubClient
    from unittest.mock import MagicMock

    mock_client = MagicMock(spec=GitHubClient)
    config = AppConfig(repositories=["pytorch/ignite"])
    scanner = IssueScanner(client=mock_client, config=config)

    res = scanner.scan_repository("https://github.com/unauthorized/repo")
    assert res.success is False
    assert "out of scope" in res.error_message.lower()
    # Ensure network calls were never made
    mock_client.list_issues.assert_not_called()
    mock_client.check_repository_access.assert_not_called()


def test_repository_scope_enforcement_in_patch_generator(tmp_path: Path):
    """Verify patch generator refuses to modify or patch another repository."""
    from bug_agent.patch_generator import PatchGenerator

    db_file = tmp_path / "scope_patch.db"
    store = TaskStore(db_path=str(db_file))

    # Task for unauthorized repository
    task, _ = store.record_discovered_task("unauthorized/repo", 99, "Bug", "http://issue/99")

    patch_gen = PatchGenerator(task_store=store)
    success, patch_rec, msg = patch_gen.generate_patch_for_task(task.task_id)
    assert success is False
    assert patch_rec is None
    assert "out of scope" in msg.lower()


def test_repository_scope_enforcement_in_publisher(tmp_path: Path):
    """Verify publisher refuses to publish or create PR for another repository."""
    from bug_agent.publisher import GitHubPublisher

    db_file = tmp_path / "scope_pub.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task("unauthorized/repo", 99, "Bug", "http://issue/99")

    publisher = GitHubPublisher(task_store=store)
    success, pub_result, msg = publisher.publish_pull_request(task.task_id)
    assert success is False
    assert pub_result is None
    assert "out of scope" in msg.lower()


def test_cli_rejects_out_of_scope_repo():
    """Verify CLI immediately rejects out-of-scope repository arguments."""
    code = cli_main(["--repo", "https://github.com/other/repo"])
    assert code != 0


def test_send_issue_notification_direct(tmp_path: Path):
    """Verify send_issue_notification sends issue directly to configured/provided Gmail."""
    db_file = tmp_path / "send_issue.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task("pytorch/ignite", 1234, "Memory leak in distributed engine", "http://issue/1234")
    mock_gmail = MockGmailClient(approver_email="myemail@gmail.com")
    config = AppConfig(repositories=["pytorch/ignite"], approver_email="myemail@gmail.com")
    mgr = ApprovalManager(task_store=store, gmail_client=mock_gmail, config=config)

    # 1. Send discovered issue
    ok, msg = mgr.send_issue_notification(task, recipient_email="myemail@gmail.com")
    assert ok is True
    assert len(mock_gmail.sent_messages) == 1
    assert mock_gmail.sent_messages[0]["to"] == "myemail@gmail.com"
    assert "#1234" in mock_gmail.sent_messages[0]["subject"]

    # 2. Analyze task and send analyzed issue notification
    provider = MockAIProvider()
    analysis = provider.analyze_defect(task.task_id, task.repository, task.issue_number, task.issue_title, task.issue_body)
    store.update_task_status(task.task_id, TaskStatus.ANALYZED, extra_fields={"analysis_result": json.dumps(analysis.to_dict())})
    analyzed_task = store.get_task(task.task_id)

    ok2, msg2 = mgr.send_issue_notification(analyzed_task, recipient_email="myemail@gmail.com")
    assert ok2 is True
    assert len(mock_gmail.sent_messages) == 2
    assert "APPROVAL REQUIRED" in mock_gmail.sent_messages[1]["subject"]


def test_cli_send_issue_command(tmp_path: Path):
    """Verify CLI --send-issue command sends issue to Gmail via CLI flags."""
    db_file = tmp_path / "cli_send.db"
    store = TaskStore(db_path=str(db_file))
    task, _ = store.record_discovered_task("pytorch/ignite", 777, "Critical deadlock on cleanup", "http://issue/777")

    code = cli_main([
        "--send-issue", str(task.issue_number),
        "--email", "user@gmail.com",
        "--db-path", str(db_file),
        "--mock-gmail",
    ])
    assert code == 0




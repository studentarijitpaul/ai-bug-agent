"""Tests for Gmail Client and Approval Manager."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from bug_agent.ai_analyzer import AnalysisResult
from bug_agent.approval_manager import ApprovalManager
from bug_agent.config import AppConfig
from bug_agent.gmail_client import GmailAuthError, GmailError, InboundEmail, MockGmailClient
from bug_agent.models import ApprovalRequest, ApprovalStatus, Task, TaskStatus
from bug_agent.task_store import TaskStore


def test_mock_gmail_send_and_queue():
    client = MockGmailClient(approver_email="alice@example.com")
    msg_id, thread_id = client.send_email(
        to="alice@example.com",
        subject="Test Subject",
        body_text="Test Body",
    )
    assert msg_id == "mock-msg-1"
    assert thread_id == "mock-thread-1"
    assert len(client.sent_messages) == 1

    # Queue reply and read back
    client.queue_reply(
        message_id="reply-1",
        sender="alice@example.com",
        subject="Re: Test Subject",
        body="APPROVE TASK-101",
    )
    inbound = client.fetch_inbound_replies()
    assert len(inbound) == 1
    assert inbound[0].sender == "alice@example.com"
    assert "APPROVE TASK-101" in inbound[0].body


def test_request_approval_transitions_to_awaiting_approval(tmp_path: Path):
    db_file = tmp_path / "test_approval.db"
    store = TaskStore(db_path=str(db_file))

    # Create task in ANALYZED status
    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=101,
        issue_title="Bug Title",
        issue_url="https://github.com/pytorch/ignite/issues/101",
    )
    analysis = AnalysisResult(
        task_id=task.task_id,
        repository=task.repository,
        issue_number=101,
        issue_summary="Sample Summary",
        is_bug=True,
        bug_classification="SUSPECTED_BUG",
        evidence_summary="Evidence",
        suspected_root_cause="Root cause",
    )
    store.update_task_status(
        task.task_id,
        TaskStatus.ANALYZED,
        extra_fields={"analysis_result": json.dumps(analysis.to_dict())},
    )
    analyzed_task = store.get_task(task.task_id)

    mock_gmail = MockGmailClient()
    config = AppConfig(
        repositories=["pytorch/ignite"],
        approver_email="dev@example.com",
        approval_timeout_hours=24,
    )
    mgr = ApprovalManager(task_store=store, gmail_client=mock_gmail, config=config)

    req = mgr.request_approval_for_task(analyzed_task)
    assert req is not None
    assert req.status == ApprovalStatus.PENDING
    assert req.approver_email == "dev@example.com"

    # Verify task state in DB is now AWAITING_APPROVAL
    updated_task = store.get_task(task.task_id)
    assert updated_task.status == TaskStatus.AWAITING_APPROVAL
    assert len(mock_gmail.sent_messages) == 1
    assert "[APPROVAL REQUIRED]" in mock_gmail.sent_messages[0]["subject"]


def test_process_valid_approval_command(tmp_path: Path):
    db_file = tmp_path / "test_valid_approval.db"
    store = TaskStore(db_path=str(db_file))

    # Setup task in AWAITING_APPROVAL
    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=102,
        issue_title="Bug Title 102",
        issue_url="https://github.com/pytorch/ignite/issues/102",
    )
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)

    expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    store.record_approval_request(
        task_id=task.task_id,
        repository=task.repository,
        issue_number=task.issue_number,
        approver_email="approver@example.com",
        expires_at=expires_at,
    )

    mock_gmail = MockGmailClient(approver_email="approver@example.com")
    config = AppConfig(repositories=["pytorch/ignite"], approver_email="approver@example.com")
    mgr = ApprovalManager(task_store=store, gmail_client=mock_gmail, config=config)

    inbound = InboundEmail(
        message_id="msg-102",
        thread_id="th-102",
        sender="approver@example.com",
        subject="Re: Approval",
        body=f"Sounds good. APPROVE {task.task_id} please.",
        date="2026-10-25T12:00:00Z",
    )

    success, msg = mgr.process_incoming_decision(inbound)
    assert success is True
    assert "APPROVED" in msg

    # Verify DB transition
    updated_task = store.get_task(task.task_id)
    assert updated_task.status == TaskStatus.APPROVED
    assert updated_task.approved_at is not None

    req = store.get_approval_request(task.task_id)
    assert req.status == ApprovalStatus.APPROVED
    assert req.decision_by == "approver@example.com"


def test_process_valid_rejection_command(tmp_path: Path):
    db_file = tmp_path / "test_rejection.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=103,
        issue_title="Bug Title 103",
        issue_url="https://github.com/pytorch/ignite/issues/103",
    )
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    store.record_approval_request(
        task_id=task.task_id,
        repository=task.repository,
        issue_number=task.issue_number,
        approver_email="approver@example.com",
        expires_at=expires_at,
    )

    mock_gmail = MockGmailClient()
    config = AppConfig(repositories=["pytorch/ignite"], approver_email="approver@example.com")
    mgr = ApprovalManager(task_store=store, gmail_client=mock_gmail, config=config)

    inbound = InboundEmail(
        message_id="msg-103",
        thread_id="th-103",
        sender="approver@example.com",
        subject="Re: Bug",
        body=f"REJECT {task.task_id}",
        date="2026-10-25T12:00:00Z",
    )

    success, msg = mgr.process_incoming_decision(inbound)
    assert success is True
    assert "REJECTED" in msg

    updated = store.get_task(task.task_id)
    assert updated.status == TaskStatus.REJECTED


def test_replay_and_duplicate_message_prevention(tmp_path: Path):
    db_file = tmp_path / "test_replay.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=104,
        issue_title="Bug 104",
        issue_url="https://github.com/pytorch/ignite/issues/104",
    )
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.record_approval_request(
        task_id=task.task_id,
        repository=task.repository,
        issue_number=task.issue_number,
        approver_email="approver@example.com",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
    )

    mock_gmail = MockGmailClient()
    config = AppConfig(repositories=["pytorch/ignite"], approver_email="approver@example.com")
    mgr = ApprovalManager(task_store=store, gmail_client=mock_gmail, config=config)

    inbound = InboundEmail(
        message_id="msg-same-id",
        thread_id="th-same",
        sender="approver@example.com",
        subject="Subject",
        body=f"APPROVE {task.task_id}",
        date="2026-10-25T12:00:00Z",
    )

    # First attempt: succeeds
    ok1, msg1 = mgr.process_incoming_decision(inbound)
    assert ok1 is True

    # Replay attempt with same message_id: rejected
    ok2, msg2 = mgr.process_incoming_decision(inbound)
    assert ok2 is False
    assert "replay ignored" in msg2.lower()


def test_unauthorized_sender_rejected(tmp_path: Path):
    db_file = tmp_path / "test_unauth.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=105,
        issue_title="Bug 105",
        issue_url="https://github.com/pytorch/ignite/issues/105",
    )
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)

    mock_gmail = MockGmailClient()
    config = AppConfig(repositories=["pytorch/ignite"], approver_email="authorized@example.com")
    mgr = ApprovalManager(task_store=store, gmail_client=mock_gmail, config=config)

    inbound = InboundEmail(
        message_id="msg-unauth",
        thread_id="th-unauth",
        sender="attacker@evil.com",
        subject="Re: Approve",
        body=f"APPROVE {task.task_id}",
        date="2026-10-25T12:00:00Z",
    )

    ok, msg = mgr.process_incoming_decision(inbound)
    assert ok is False
    assert "unauthorized sender" in msg.lower()

    # Task must NOT be approved
    task_after = store.get_task(task.task_id)
    assert task_after.status == TaskStatus.AWAITING_APPROVAL


def test_expired_approval_rejected(tmp_path: Path):
    db_file = tmp_path / "test_expired.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=106,
        issue_title="Bug 106",
        issue_url="https://github.com/pytorch/ignite/issues/106",
    )
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)

    # Already expired 2 hours ago
    past_expiration = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store.record_approval_request(
        task_id=task.task_id,
        repository=task.repository,
        issue_number=task.issue_number,
        approver_email="approver@example.com",
        expires_at=past_expiration,
    )

    mock_gmail = MockGmailClient()
    config = AppConfig(repositories=["pytorch/ignite"], approver_email="approver@example.com")
    mgr = ApprovalManager(task_store=store, gmail_client=mock_gmail, config=config)

    inbound = InboundEmail(
        message_id="msg-expired",
        thread_id="th-expired",
        sender="approver@example.com",
        subject="Subject",
        body=f"APPROVE {task.task_id}",
        date="2026-10-25T12:00:00Z",
    )

    ok, msg = mgr.process_incoming_decision(inbound)
    assert ok is False
    assert "expired" in msg.lower()

    # Task transitioned to EXPIRED
    task_after = store.get_task(task.task_id)
    assert task_after.status == TaskStatus.EXPIRED


def test_malformed_command_rejected(tmp_path: Path):
    db_file = tmp_path / "test_malformed.db"
    store = TaskStore(db_path=str(db_file))

    mock_gmail = MockGmailClient()
    config = AppConfig(repositories=["pytorch/ignite"], approver_email="approver@example.com")
    mgr = ApprovalManager(task_store=store, gmail_client=mock_gmail, config=config)

    inbound = InboundEmail(
        message_id="msg-malformed",
        thread_id="th-malformed",
        sender="approver@example.com",
        subject="Re: Issue",
        body="Yes, this looks great please fix it!",  # Missing strict command
        date="2026-10-25T12:00:00Z",
    )

    ok, msg = mgr.process_incoming_decision(inbound)
    assert ok is False
    assert "no valid approval command" in msg.lower()

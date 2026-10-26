"""Tests for publication approval, reviewer email formatting, and GitHubPublisher."""

import json
from pathlib import Path
import pytest
from unittest.mock import MagicMock

from bug_agent.approval_manager import ApprovalManager
from bug_agent.config import AppConfig
from bug_agent.gmail_client import InboundEmail, MockGmailClient
from bug_agent.models import Task, TaskStatus
from bug_agent.publisher import GitHubPublisher
from bug_agent.task_store import TaskStore
from bug_agent.test_runner import DiffVerifier


def test_request_publication_approval_for_tested_task(tmp_path: Path):
    db_file = tmp_path / "pub_appr.db"
    store = TaskStore(db_path=str(db_file))

    # Task in TESTED status
    task, _ = store.record_discovered_task("pytorch/ignite", 101, "Bug Title", "http://issue/101")
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.APPROVED)

    patch_file = tmp_path / "valid.patch"
    patch_file.write_text("--- a/module.py\n+++ b/module.py\n@@ -1 +1 @@\n+# Fix\n", encoding="utf-8")
    store.update_task_status(task.task_id, TaskStatus.PATCH_READY, extra_fields={"patch_path": str(patch_file), "base_commit_sha": "sha123"})

    test_res = {"exit_code": 0, "test_summary": "Passed", "duration_seconds": 1.2}
    store.update_task_status(task.task_id, TaskStatus.TESTED, extra_fields={"test_result": json.dumps(test_res)})
    tested_task = store.get_task(task.task_id)

    mock_gmail = MockGmailClient(approver_email="approver@example.com")
    config = AppConfig(repositories=["pytorch/ignite"], approver_email="approver@example.com")
    mgr = ApprovalManager(task_store=store, gmail_client=mock_gmail, config=config)

    ok = mgr.request_publication_approval_for_task(tested_task)
    assert ok is True

    # Verify task state in DB is AWAITING_PUBLICATION_APPROVAL
    updated = store.get_task(task.task_id)
    assert updated.status == TaskStatus.AWAITING_PUBLICATION_APPROVAL

    pub_rec = store.get_publication_record(task.task_id)
    assert pub_rec is not None
    assert pub_rec["status"] == "AWAITING_PUBLICATION_APPROVAL"
    assert len(mock_gmail.sent_messages) == 1
    assert "PUBLICATION APPROVAL REQUIRED" in mock_gmail.sent_messages[0]["subject"]


def test_process_publication_approval_decision(tmp_path: Path):
    db_file = tmp_path / "pub_decision.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task("pytorch/ignite", 102, "Bug 102", "http://issue/102")
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.APPROVED)
    store.update_task_status(task.task_id, TaskStatus.PATCH_READY)
    store.update_task_status(task.task_id, TaskStatus.TESTED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_PUBLICATION_APPROVAL)

    store.record_publication_request(
        task_id=task.task_id,
        repository=task.repository,
        base_commit_sha="base-sha",
        patch_hash="patch-hash",
        approver_email="approver@example.com",
        expires_at="2026-10-27T00:00:00Z",
    )

    mock_gmail = MockGmailClient(approver_email="approver@example.com")
    config = AppConfig(repositories=["pytorch/ignite"], approver_email="approver@example.com")
    mgr = ApprovalManager(task_store=store, gmail_client=mock_gmail, config=config)

    inbound = InboundEmail(
        message_id="msg-pub-approve",
        thread_id="th-pub",
        sender="approver@example.com",
        subject="Re: Approval",
        body=f"APPROVE PUBLISH {task.task_id}",
        date="2026-10-25T12:00:00Z",
    )

    success, msg = mgr.process_incoming_decision(inbound)
    assert success is True
    assert "PUBLICATION_APPROVED" in msg

    pub_rec = store.get_publication_record(task.task_id)
    assert pub_rec["status"] == "PUBLICATION_APPROVED"


def test_process_publication_rejection_decision(tmp_path: Path):
    db_file = tmp_path / "pub_rej.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task("pytorch/ignite", 103, "Bug 103", "http://issue/103")
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.APPROVED)
    store.update_task_status(task.task_id, TaskStatus.PATCH_READY)
    store.update_task_status(task.task_id, TaskStatus.TESTED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_PUBLICATION_APPROVAL)

    store.record_publication_request(
        task_id=task.task_id,
        repository=task.repository,
        base_commit_sha="base-sha",
        patch_hash="patch-hash",
        approver_email="approver@example.com",
        expires_at="2026-10-27T00:00:00Z",
    )

    mock_gmail = MockGmailClient(approver_email="approver@example.com")
    config = AppConfig(repositories=["pytorch/ignite"], approver_email="approver@example.com")
    mgr = ApprovalManager(task_store=store, gmail_client=mock_gmail, config=config)

    inbound = InboundEmail(
        message_id="msg-pub-reject",
        thread_id="th-pub-rej",
        sender="approver@example.com",
        subject="Re: Approval",
        body=f"REJECT PUBLISH {task.task_id}",
        date="2026-10-25T12:00:00Z",
    )

    success, msg = mgr.process_incoming_decision(inbound)
    assert success is True
    assert "REJECTED" in msg

    updated = store.get_task(task.task_id)
    assert updated.status == TaskStatus.REJECTED


def test_github_publisher_successful_simulation(tmp_path: Path):
    db_file = tmp_path / "pub_run.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task("pytorch/ignite", 104, "Bug 104", "http://issue/104")
    patch_file = tmp_path / "patch.diff"
    patch_text = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n+# Fix\n"
    patch_file.write_text(patch_text, encoding="utf-8")
    patch_hash = DiffVerifier.compute_patch_hash(patch_text)

    test_res = {"exit_code": 0, "test_summary": "Passed"}

    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.APPROVED)
    store.update_task_status(task.task_id, TaskStatus.PATCH_READY, extra_fields={"patch_path": str(patch_file), "base_commit_sha": "abc"})
    store.update_task_status(task.task_id, TaskStatus.TESTED, extra_fields={"test_result": json.dumps(test_res)})
    store.update_task_status(task.task_id, TaskStatus.AWAITING_PUBLICATION_APPROVAL)

    store.record_publication_request(task.task_id, task.repository, "abc", patch_hash, "approver@example.com", "2026-10-27T00:00:00Z")
    store.record_publication_decision(task.task_id, approved=True, approver="approver@example.com")

    publisher = GitHubPublisher(task_store=store, config=AppConfig(repositories=["pytorch/ignite"]))
    success, result, msg = publisher.publish_pull_request(task.task_id)

    assert success is True
    assert result is not None
    assert result.pr_number > 0
    assert "pull/" in result.pr_url

    # Check task transitioned to PUBLISHED
    updated = store.get_task(task.task_id)
    assert updated.status == TaskStatus.PUBLISHED
    assert updated.publication_approved_at is not None


def test_publisher_rejects_unapproved_tasks(tmp_path: Path):
    db_file = tmp_path / "pub_unapproved.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task("pytorch/ignite", 105, "Bug 105", "http://issue/105")
    publisher = GitHubPublisher(task_store=store)

    success, result, msg = publisher.publish_pull_request(task.task_id)
    assert success is False
    assert "Expected AWAITING_PUBLICATION_APPROVAL" in msg

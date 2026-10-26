"""Security and integrity test suite for Multi-Repository AI Bug Agent.

Phase D: Security and integrity audit.
Phase E: Database integrity, concurrency, and recovery checks.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bug_agent.approval_manager import ApprovalManager
from bug_agent.config import AppConfig
from bug_agent.gmail_client import InboundEmail, MockGmailClient
from bug_agent.models import Task, TaskStatus
from bug_agent.patch_generator import (
    MAX_DIFF_BYTES,
    MAX_MODIFIED_FILES,
    PatchGenerator,
    SecurityViolationError,
    WorkspaceManager,
)
from bug_agent.publisher import GitHubPublisher
from bug_agent.task_store import InvalidStateTransitionError, TaskStore
from bug_agent.test_runner import (
    DiffVerifier,
    DockerTestRunner,
    MockTestRunner,
    SandboxUnavailableError,
    SandboxedVerifier,
)


# =============================================================================
# 1. Approval Security Tests
# =============================================================================

def test_investigation_approval_cannot_authorize_publication(tmp_path: Path):
    """APPROVE TASK-... cannot authorize a task awaiting publication approval."""
    store = TaskStore(db_path=str(tmp_path / "appr1.db"))
    task, _ = store.record_discovered_task("pytorch/ignite", 1, "Issue 1", "http://issue/1")
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.APPROVED)
    store.update_task_status(task.task_id, TaskStatus.PATCH_READY)
    store.update_task_status(task.task_id, TaskStatus.TESTED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_PUBLICATION_APPROVAL)

    mgr = ApprovalManager(
        task_store=store,
        gmail_client=MockGmailClient(approver_email="lead@example.com"),
        config=AppConfig(repositories=["pytorch/ignite"], approver_email="lead@example.com"),
    )

    inbound = InboundEmail(
        message_id="msg-cross-1",
        thread_id="th-1",
        sender="lead@example.com",
        subject="Re: Approval",
        body=f"APPROVE {task.task_id}",  # Investigation command, not publication
        date="2026-10-25T12:00:00Z",
    )
    ok, msg = mgr.process_incoming_decision(inbound)
    assert ok is False
    assert "not AWAITING_APPROVAL" in msg
    assert store.get_task(task.task_id).status == TaskStatus.AWAITING_PUBLICATION_APPROVAL


def test_publication_approval_cannot_authorize_investigation(tmp_path: Path):
    """APPROVE PUBLISH TASK-... cannot authorize a task awaiting initial investigation approval."""
    store = TaskStore(db_path=str(tmp_path / "appr2.db"))
    task, _ = store.record_discovered_task("pytorch/ignite", 2, "Issue 2", "http://issue/2")
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)

    mgr = ApprovalManager(
        task_store=store,
        gmail_client=MockGmailClient(approver_email="lead@example.com"),
        config=AppConfig(repositories=["pytorch/ignite"], approver_email="lead@example.com"),
    )

    inbound = InboundEmail(
        message_id="msg-cross-2",
        thread_id="th-2",
        sender="lead@example.com",
        subject="Re: Approval",
        body=f"APPROVE PUBLISH {task.task_id}",
        date="2026-10-25T12:00:00Z",
    )
    ok, msg = mgr.process_incoming_decision(inbound)
    assert ok is False
    assert "not AWAITING_PUBLICATION_APPROVAL" in msg
    assert store.get_task(task.task_id).status == TaskStatus.AWAITING_APPROVAL


def test_conflicting_commands_in_email_rejected(tmp_path: Path):
    """An email containing both APPROVE and REJECT commands must be rejected as ambiguous."""
    store = TaskStore(db_path=str(tmp_path / "conflict.db"))
    task, _ = store.record_discovered_task("pytorch/ignite", 3, "Issue 3", "http://issue/3")
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)

    mgr = ApprovalManager(
        task_store=store,
        gmail_client=MockGmailClient(approver_email="lead@example.com"),
        config=AppConfig(repositories=["pytorch/ignite"], approver_email="lead@example.com"),
    )

    inbound = InboundEmail(
        message_id="msg-conflict",
        thread_id="th-3",
        sender="lead@example.com",
        subject="Re: Approval",
        body=f"APPROVE {task.task_id} and also REJECT {task.task_id}",
        date="2026-10-25T12:00:00Z",
    )
    ok, msg = mgr.process_incoming_decision(inbound)
    assert ok is False
    assert "conflicting" in msg.lower()


# =============================================================================
# 2. Patch and Filesystem Security Tests
# =============================================================================

def test_patch_generator_rejects_drive_letters_and_workflows(tmp_path: Path):
    """WorkspaceManager must reject Windows drive letters and GitHub workflows modifications."""
    wm = WorkspaceManager()
    ws = tmp_path / "ws_sec"
    ws.mkdir()

    # Windows drive letter
    with pytest.raises(SecurityViolationError, match="Path traversal"):
        wm.validate_file_path(ws, "C:/Windows/System32/cmd.exe")

    # GitHub workflow path
    with pytest.raises(SecurityViolationError, match="protected file/directory forbidden"):
        wm.validate_file_path(ws, ".github/workflows/ci.yml")

    # Environment file
    with pytest.raises(SecurityViolationError, match="protected file/directory forbidden"):
        wm.validate_file_path(ws, ".env")


def test_diff_verifier_validates_both_source_and_dest_paths(tmp_path: Path):
    """DiffVerifier must reject patches where either source (--- a/) or dest (+++ b/) traverses directories."""
    malicious_patch = tmp_path / "traversal.patch"
    malicious_patch.write_text(
        "--- a/../../secret.txt\n+++ b/module.py\n@@ -1 +1 @@\n+# Fix\n",
        encoding="utf-8",
    )
    safe, reason, files = DiffVerifier.verify_patch_safety(malicious_patch)
    assert safe is False
    assert "security violation" in reason.lower()


def test_workspace_cleanup_preserves_external_directories(tmp_path: Path):
    """Workspace cleanup must only remove the designated temporary workspace."""
    wm = WorkspaceManager(workspace_root=str(tmp_path / "workspaces"))
    user_file = tmp_path / "important_user_code.py"
    user_file.write_text("print('user')", encoding="utf-8")

    ws = wm.create_workspace(prefix="clean_test_")
    assert ws.exists()

    wm.cleanup_workspace(ws)
    assert not ws.exists()
    # External user file remains intact
    assert user_file.exists()


# =============================================================================
# 3. Sandbox Security Tests
# =============================================================================

def test_docker_runner_configuration_security_flags():
    """DockerTestRunner enforces disabled network and memory/CPU limits."""
    runner = DockerTestRunner(memory_limit="512m", cpu_limit="0.5", network_enabled=False)
    assert runner.network_enabled is False
    assert runner.memory_limit == "512m"
    assert runner.cpu_limit == "0.5"


def test_sandbox_secret_scrubbing():
    """SandboxedVerifier must scrub tokens and API keys from test stdout and stderr."""
    raw_output = (
        "Running tests...\n"
        "DEBUG: Token loaded: ghp_123456789012345678901234567890123456\n"
        "ERROR: Authorization: Bearer secret_oauth_token_val_123\n"
        "PASSED 10 tests\n"
    )
    sanitized = SandboxedVerifier.sanitize_output(raw_output)
    assert "ghp_123456789012345678901234567890123456" not in sanitized
    assert "[REDACTED_GITHUB_TOKEN]" in sanitized
    assert "secret_oauth_token_val_123" not in sanitized
    assert "[REDACTED_AUTH]" in sanitized


# =============================================================================
# 4. GitHub Publication Security Tests
# =============================================================================

def test_publisher_rejects_repo_and_base_sha_mismatches(tmp_path: Path):
    """GitHubPublisher must stop publication if the repository or base commit SHA was altered."""
    store = TaskStore(db_path=str(tmp_path / "pub_sec.db"))
    task, _ = store.record_discovered_task("pytorch/ignite", 10, "Bug 10", "http://issue/10")

    patch_file = tmp_path / "patch.diff"
    patch_text = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n+# Fix\n"
    patch_file.write_text(patch_text, encoding="utf-8")
    patch_hash = DiffVerifier.compute_patch_hash(patch_text)

    test_res = {"exit_code": 0, "classification": "TESTED_PASSED"}
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.APPROVED)
    store.update_task_status(task.task_id, TaskStatus.PATCH_READY, extra_fields={"patch_path": str(patch_file), "base_commit_sha": "sha_orig"})
    store.update_task_status(task.task_id, TaskStatus.TESTED, extra_fields={"test_result": json.dumps(test_res)})
    store.update_task_status(task.task_id, TaskStatus.AWAITING_PUBLICATION_APPROVAL)

    # Authorized with different base SHA
    store.record_publication_request(task.task_id, task.repository, "sha_altered", patch_hash, "approver@example.com", "2026-10-27T00:00:00Z")
    store.record_publication_decision(task.task_id, approved=True, approver="approver@example.com")

    publisher = GitHubPublisher(task_store=store, config=AppConfig(repositories=["pytorch/ignite"]))
    success, result, msg = publisher.publish_pull_request(task.task_id)

    assert success is False
    assert "Base commit SHA mismatch" in msg


def test_publisher_dry_run_leaves_database_state_intact(tmp_path: Path):
    """Dry run publication must validate without changing database state to PUBLISHED."""
    store = TaskStore(db_path=str(tmp_path / "dry_run.db"))
    task, _ = store.record_discovered_task("pytorch/ignite", 20, "Bug 20", "http://issue/20")

    patch_file = tmp_path / "p.diff"
    patch_text = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+# Fix\n"
    patch_file.write_text(patch_text, encoding="utf-8")
    patch_hash = DiffVerifier.compute_patch_hash(patch_text)

    test_res = {"exit_code": 0, "classification": "TESTED_PASSED"}
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.APPROVED)
    store.update_task_status(task.task_id, TaskStatus.PATCH_READY, extra_fields={"patch_path": str(patch_file), "base_commit_sha": "abc"})
    store.update_task_status(task.task_id, TaskStatus.TESTED, extra_fields={"test_result": json.dumps(test_res)})
    store.update_task_status(task.task_id, TaskStatus.AWAITING_PUBLICATION_APPROVAL)

    store.record_publication_request(task.task_id, task.repository, "abc", patch_hash, "approver@example.com", "2026-10-27T00:00:00Z")
    store.record_publication_decision(task.task_id, approved=True, approver="approver@example.com")

    publisher = GitHubPublisher(task_store=store, config=AppConfig(repositories=["pytorch/ignite"]))
    success, result, msg = publisher.publish_pull_request(task.task_id, dry_run=True)

    assert success is True
    assert "[DRY-RUN]" in msg
    # Task remains in AWAITING_PUBLICATION_APPROVAL
    task_after = store.get_task(task.task_id)
    assert task_after.status == TaskStatus.AWAITING_PUBLICATION_APPROVAL


# =============================================================================
# 5. Database & Recovery Checks
# =============================================================================

def test_database_sweep_expired_tasks(tmp_path: Path):
    """ApprovalManager.sweep_expired_tasks transitions stale tasks to EXPIRED."""
    store = TaskStore(db_path=str(tmp_path / "sweep.db"))
    task, _ = store.record_discovered_task("pytorch/ignite", 30, "Bug 30", "http://issue/30")
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)

    # Expired 2 hours ago
    past_iso = "2020-01-01T00:00:00Z"
    store.record_approval_request(
        task_id=task.task_id,
        repository=task.repository,
        issue_number=30,
        approver_email="dev@example.com",
        expires_at=past_iso,
    )

    mgr = ApprovalManager(
        task_store=store,
        gmail_client=MockGmailClient(approver_email="dev@example.com"),
        config=AppConfig(repositories=["pytorch/ignite"], approver_email="dev@example.com"),
    )
    swept = mgr.sweep_expired_tasks()

    assert task.task_id in swept
    task_after = store.get_task(task.task_id)
    assert task_after.status == TaskStatus.EXPIRED


def test_illegal_state_transition_from_published_fails(tmp_path: Path):
    """Tasks in terminal state PUBLISHED cannot be transitioned to any other state."""
    store = TaskStore(db_path=str(tmp_path / "terminal.db"))
    task, _ = store.record_discovered_task("pytorch/ignite", 40, "Bug 40", "http://issue/40")
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.APPROVED)
    store.update_task_status(task.task_id, TaskStatus.PATCH_READY)
    store.update_task_status(task.task_id, TaskStatus.TESTED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_PUBLICATION_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.PUBLISHED)

    # Attempt to transition from PUBLISHED back to DISCOVERED or APPROVED
    with pytest.raises(InvalidStateTransitionError):
        store.update_task_status(task.task_id, TaskStatus.DISCOVERED)

    with pytest.raises(InvalidStateTransitionError):
        store.update_task_status(task.task_id, TaskStatus.APPROVED)

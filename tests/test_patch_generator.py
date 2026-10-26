"""Tests for WorkspaceManager and PatchGenerator."""

import json
from pathlib import Path
import pytest
from unittest.mock import MagicMock

from bug_agent.ai_analyzer import AIProviderError, AnalysisResult, FileEvidence, MockAIProvider
from bug_agent.config import AppConfig
from bug_agent.models import ApprovalRequest, ApprovalStatus, Task, TaskStatus
from bug_agent.patch_generator import (
    MAX_DIFF_BYTES,
    MAX_MODIFIED_FILES,
    PatchGenerator,
    SecurityViolationError,
    WorkspaceManager,
)
from bug_agent.task_store import TaskStore


def test_workspace_creation_and_cleanup(tmp_path: Path):
    wm = WorkspaceManager(workspace_root=str(tmp_path))
    ws = wm.create_workspace(prefix="test_ws_")
    assert ws.exists()
    assert ws.is_dir()

    wm.cleanup_workspace(ws)
    assert not ws.exists()


def test_validate_file_path_security(tmp_path: Path):
    wm = WorkspaceManager()
    ws = tmp_path / "sandbox"
    ws.mkdir()

    # Valid relative path
    valid_path = wm.validate_file_path(ws, "src/module.py")
    assert valid_path == (ws / "src/module.py").resolve()

    # Path traversal with ..
    with pytest.raises(SecurityViolationError, match="Path traversal"):
        wm.validate_file_path(ws, "../outside.py")

    # Leading slash
    with pytest.raises(SecurityViolationError, match="Path traversal"):
        wm.validate_file_path(ws, "/etc/passwd")

    # Protected file (.env)
    with pytest.raises(SecurityViolationError, match="protected file"):
        wm.validate_file_path(ws, ".env")

    # Protected credentials
    with pytest.raises(SecurityViolationError, match="protected file"):
        wm.validate_file_path(ws, "credentials.json")

    # Protected binary extension (.exe)
    with pytest.raises(SecurityViolationError, match="protected extension"):
        wm.validate_file_path(ws, "bin/malicious.exe")


def test_unified_diff_generation():
    orig = {"file.py": "def foo():\n    return 1\n"}
    mod = {"file.py": "def foo():\n    return 2\n"}
    diff = WorkspaceManager.generate_unified_diff(orig, mod)
    assert "--- a/file.py" in diff
    assert "+++ b/file.py" in diff
    assert "-    return 1" in diff
    assert "+    return 2" in diff


def test_patch_generation_for_approved_task(tmp_path: Path):
    db_file = tmp_path / "patch_test.db"
    store = TaskStore(db_path=str(db_file))

    # Setup task in APPROVED status with valid approval request
    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=101,
        issue_title="Bug in trainer",
        issue_url="https://github.com/pytorch/ignite/issues/101",
    )
    analysis = AnalysisResult(
        task_id=task.task_id,
        repository=task.repository,
        issue_number=101,
        issue_summary="Trainer crashes on batch size 0",
        is_bug=True,
        bug_classification="SUSPECTED_BUG",
        evidence_summary="Division by zero",
        suspected_root_cause="Missing batch check",
        relevant_files=[FileEvidence(path="ignite/engine/trainer.py", snippet="def train():\n    pass\n")],
        suggested_fix="Add validation for batch size",
    )
    store.update_task_status(task.task_id, TaskStatus.ANALYZED, extra_fields={"analysis_result": json.dumps(analysis.to_dict())})
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)

    # Approve task
    store.record_approval_request(
        task_id=task.task_id,
        repository=task.repository,
        issue_number=101,
        approver_email="dev@example.com",
        expires_at="2026-10-26T00:00:00Z",
    )
    store.record_approval_decision(task.task_id, approved=True, approver="dev@example.com")

    # Generate patch
    generator = PatchGenerator(task_store=store, ai_provider=MockAIProvider())
    success, record, message = generator.generate_patch_for_task(task.task_id)

    assert success is True
    assert record is not None
    assert "ignite/engine/trainer.py" in record.modified_files
    assert "--- a/ignite/engine/trainer.py" in record.diff_content
    assert "# Fix applied for TASK-pytorch-ignite-101" in record.diff_content

    # Verify task transitioned to PATCH_READY in DB
    updated_task = store.get_task(task.task_id)
    assert updated_task.status == TaskStatus.PATCH_READY
    assert updated_task.patch_path is not None


def test_reject_unapproved_tasks(tmp_path: Path):
    db_file = tmp_path / "unapproved_test.db"
    store = TaskStore(db_path=str(db_file))

    # Task in DISCOVERED
    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=201,
        issue_title="Unapproved bug",
        issue_url="https://github.com/pytorch/ignite/issues/201",
    )
    generator = PatchGenerator(task_store=store)
    success, record, message = generator.generate_patch_for_task(task.task_id)

    assert success is False
    assert record is None
    assert "Only APPROVED tasks can be patched" in message


def test_reject_rejected_and_expired_tasks(tmp_path: Path):
    db_file = tmp_path / "rejected_test.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=202,
        issue_title="Rejected bug",
        issue_url="https://github.com/pytorch/ignite/issues/202",
    )
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.REJECTED)

    generator = PatchGenerator(task_store=store)
    success, record, message = generator.generate_patch_for_task(task.task_id)
    assert success is False
    assert "Only APPROVED tasks can be patched" in message


def test_ai_provider_error_handling(tmp_path: Path):
    db_file = tmp_path / "provider_err_test.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=301,
        issue_title="Bug",
        issue_url="https://github.com/pytorch/ignite/issues/301",
    )
    analysis = AnalysisResult(
        task_id=task.task_id,
        repository=task.repository,
        issue_number=301,
        issue_summary="Crash",
        is_bug=True,
        bug_classification="SUSPECTED_BUG",
        evidence_summary="Traceback",
        suspected_root_cause="Bug",
    )
    store.update_task_status(task.task_id, TaskStatus.ANALYZED, extra_fields={"analysis_result": json.dumps(analysis.to_dict())})
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.record_approval_request(task.task_id, task.repository, 301, "dev@example.com", "2026-10-26T00:00:00Z")
    store.record_approval_decision(task.task_id, approved=True, approver="dev@example.com")

    failing_provider = MockAIProvider(failure_mode="timeout")
    generator = PatchGenerator(task_store=store, ai_provider=failing_provider)
    success, record, message = generator.generate_patch_for_task(task.task_id)

    assert success is False
    assert "timeout" in message.lower()
    # Task remains in APPROVED status (not corrupted)
    task_after = store.get_task(task.task_id)
    assert task_after.status == TaskStatus.APPROVED

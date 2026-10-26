"""Unit tests for SQLite TaskStore and state machine transitions."""

import sqlite3
import pytest
from pathlib import Path

from bug_agent.models import Task, TaskStatus
from bug_agent.task_store import InvalidStateTransitionError, TaskStore


def test_task_store_initialization(tmp_path: Path):
    db_file = tmp_path / "test_tasks.db"
    store = TaskStore(db_path=str(db_file))

    assert db_file.exists()

    # Verify tables and index exist
    with store._get_connection() as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
        assert "tasks" in tables


def test_record_and_retrieve_task(tmp_path: Path):
    db_file = tmp_path / "test_tasks.db"
    store = TaskStore(db_path=str(db_file))

    task, is_new = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=101,
        issue_title="Sample Ignite Bug",
        issue_url="https://github.com/pytorch/ignite/issues/101",
        issue_body="Reproduction steps here",
        labels=["bug", "core"],
        author="testuser",
    )

    assert is_new is True
    assert task.repository == "pytorch/ignite"
    assert task.issue_number == 101
    assert task.status == TaskStatus.DISCOVERED
    assert task.labels == ["bug", "core"]
    assert task.author == "testuser"
    assert task.task_id == "TASK-pytorch-ignite-101"

    # Fetch by task_id
    retrieved = store.get_task(task.task_id)
    assert retrieved is not None
    assert retrieved.task_id == task.task_id
    assert retrieved.issue_title == "Sample Ignite Bug"

    # Fetch by repo and issue number
    by_repo_issue = store.get_task_by_repo_issue("pytorch/ignite", 101)
    assert by_repo_issue is not None
    assert by_repo_issue.task_id == task.task_id


def test_idempotent_duplicate_recording(tmp_path: Path):
    db_file = tmp_path / "test_tasks.db"
    store = TaskStore(db_path=str(db_file))

    task1, is_new1 = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=202,
        issue_title="Bug Title",
        issue_url="https://github.com/pytorch/ignite/issues/202",
    )
    assert is_new1 is True

    # Attempt to insert same task again
    task2, is_new2 = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=202,
        issue_title="Updated Bug Title",
        issue_url="https://github.com/pytorch/ignite/issues/202",
    )
    assert is_new2 is False
    assert task1.task_id == task2.task_id

    # Count tasks in DB
    all_tasks = store.list_tasks(repository="pytorch/ignite")
    assert len(all_tasks) == 1


def test_valid_state_transitions(tmp_path: Path):
    db_file = tmp_path / "test_tasks.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=303,
        issue_title="State Bug",
        issue_url="https://github.com/pytorch/ignite/issues/303",
    )
    assert task.status == TaskStatus.DISCOVERED

    # DISCOVERED -> ANALYZED
    updated = store.update_task_status(task.task_id, TaskStatus.ANALYZED, extra_fields={"analysis_result": "Root cause found"})
    assert updated.status == TaskStatus.ANALYZED
    assert updated.analysis_result == "Root cause found"

    # ANALYZED -> AWAITING_APPROVAL
    updated = store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    assert updated.status == TaskStatus.AWAITING_APPROVAL

    # AWAITING_APPROVAL -> APPROVED
    updated = store.update_task_status(task.task_id, TaskStatus.APPROVED)
    assert updated.status == TaskStatus.APPROVED

    # APPROVED -> PATCH_READY
    updated = store.update_task_status(task.task_id, TaskStatus.PATCH_READY, extra_fields={"patch_path": "/tmp/patch.diff"})
    assert updated.status == TaskStatus.PATCH_READY

    # PATCH_READY -> TESTED
    updated = store.update_task_status(task.task_id, TaskStatus.TESTED, extra_fields={"test_result": "PASSED"})
    assert updated.status == TaskStatus.TESTED

    # TESTED -> AWAITING_PUBLICATION_APPROVAL
    updated = store.update_task_status(task.task_id, TaskStatus.AWAITING_PUBLICATION_APPROVAL)
    assert updated.status == TaskStatus.AWAITING_PUBLICATION_APPROVAL

    # AWAITING_PUBLICATION_APPROVAL -> PUBLISHED
    updated = store.update_task_status(task.task_id, TaskStatus.PUBLISHED)
    assert updated.status == TaskStatus.PUBLISHED


def test_invalid_state_transition_raises_error(tmp_path: Path):
    db_file = tmp_path / "test_tasks.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=404,
        issue_title="Illegal Transition Bug",
        issue_url="https://github.com/pytorch/ignite/issues/404",
    )

    # Cannot transition directly from DISCOVERED to PUBLISHED
    with pytest.raises(InvalidStateTransitionError):
        store.update_task_status(task.task_id, TaskStatus.PUBLISHED)


def test_reset_failed_task(tmp_path: Path):
    db_file = tmp_path / "test_tasks.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=505,
        issue_title="Failing Task",
        issue_url="https://github.com/pytorch/ignite/issues/505",
    )

    # Fail the task
    store.update_task_status(task.task_id, TaskStatus.FAILED, error_message="Network timeout")
    failed = store.get_task(task.task_id)
    assert failed.status == TaskStatus.FAILED
    assert failed.error_message == "Network timeout"

    # Reset task for retry
    reset = store.reset_failed_task(task.task_id)
    assert reset.status == TaskStatus.DISCOVERED
    assert reset.error_message is None


def test_task_store_connection_lifecycle_and_cleanup_on_windows(tmp_path: Path):
    """Regression test: verify TaskStore releases SQLite file locks so files can be removed on Windows."""
    db_file = tmp_path / "lifecycle_cleanup.db"
    store = TaskStore(db_path=str(db_file))

    # Perform multiple operations that open connections
    task, _ = store.record_discovered_task("pytorch/ignite", 999, "Lifecycle Bug", "http://issue/999")
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    _ = store.get_task(task.task_id)
    _ = store.list_tasks(repository="pytorch/ignite")

    with store._get_connection() as conn:
        _ = conn.execute("SELECT count(*) FROM tasks").fetchone()

    # Direct verification: The database file MUST be removable immediately without WinError 32 PermissionError
    db_file.unlink()
    assert not db_file.exists()

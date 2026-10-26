"""Tests for DefectWorkflow integration with task store and state transitions."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from bug_agent.ai_analyzer import AIProviderError, MockAIProvider
from bug_agent.github_client import GitHubClient
from bug_agent.models import Task, TaskStatus
from bug_agent.task_store import TaskStore
from bug_agent.workflow import DefectWorkflow


def test_workflow_successful_analysis(tmp_path: Path):
    db_file = tmp_path / "workflow_test.db"
    store = TaskStore(db_path=str(db_file))

    # Record discovered task
    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=101,
        issue_title="TypeError: engine run fails with custom metric",
        issue_url="https://github.com/pytorch/ignite/issues/101",
        issue_body="In ignite/engine/engine.py: Traceback occurred.",
    )
    assert task.status == TaskStatus.DISCOVERED

    # Mock github client to return sample file content
    mock_gh = MagicMock(spec=GitHubClient)
    mock_gh.get_file_content.return_value = "def run(): pass"

    workflow = DefectWorkflow(
        task_store=store,
        ai_provider=MockAIProvider(),
        github_client=mock_gh,
    )

    result = workflow.analyze_task(task)
    assert result is not None
    assert result.is_bug is True
    assert result.task_id == task.task_id

    # Verify state transition to ANALYZED in store
    updated_task = store.get_task(task.task_id)
    assert updated_task.status == TaskStatus.ANALYZED
    assert updated_task.analysis_result is not None
    assert "SUSPECTED_BUG" in updated_task.analysis_result


def test_workflow_provider_error_transitions_to_failed(tmp_path: Path):
    db_file = tmp_path / "workflow_error.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=202,
        issue_title="Crashing bug",
        issue_url="https://github.com/pytorch/ignite/issues/202",
        issue_body="Crash description",
    )

    failing_provider = MockAIProvider(failure_mode="timeout")
    workflow = DefectWorkflow(
        task_store=store,
        ai_provider=failing_provider,
    )

    result = workflow.analyze_task(task)
    assert result is None

    # Verify task transitioned to FAILED with error message preserved
    updated_task = store.get_task(task.task_id)
    assert updated_task.status == TaskStatus.FAILED
    assert "timeout" in updated_task.error_message.lower()


def test_workflow_skips_non_discovered_tasks(tmp_path: Path):
    db_file = tmp_path / "workflow_skip.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=303,
        issue_title="Already analyzed bug",
        issue_url="https://github.com/pytorch/ignite/issues/303",
    )
    # Transition to ANALYZED
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    analyzed_task = store.get_task(task.task_id)

    workflow = DefectWorkflow(task_store=store, ai_provider=MockAIProvider())
    result = workflow.analyze_task(analyzed_task)
    assert result is None  # Skipped


def test_candidate_file_path_extraction():
    workflow = DefectWorkflow(task_store=MagicMock())
    body = """
    We noticed a regression in ignite/engine/engine.py line 45.
    Also check tests/ignite/test_engine.py.
    Path traversal attempts like ../../etc/passwd or /root/secret.py should be ignored.
    """
    paths = workflow.extract_candidate_file_paths(body)
    assert "ignite/engine/engine.py" in paths
    assert "tests/ignite/test_engine.py" in paths
    assert "../../etc/passwd" not in paths
    assert "/root/secret.py" not in paths

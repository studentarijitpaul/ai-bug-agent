"""Tests for IssueScanner."""

import pytest
from unittest.mock import MagicMock

from bug_agent.config import AppConfig
from bug_agent.github_client import GitHubClient, GitHubNotFoundError
from bug_agent.issue_scanner import IssueScanner


def test_scan_filters_pull_requests():
    mock_client = MagicMock(spec=GitHubClient)
    mock_client.check_repository_access.return_value = {"id": 1}
    mock_client.list_issues.return_value = [
        {"number": 101, "title": "Real Bug", "state": "open"},
        {"number": 102, "title": "Pull Request Title", "state": "open", "pull_request": {"url": "https://..."}},
        {"number": 103, "title": "Another Bug", "state": "open"},
    ]

    config = AppConfig(repositories=["pytorch/ignite"], include_pull_requests=False)
    scanner = IssueScanner(client=mock_client, config=config)

    result = scanner.scan_repository("pytorch/ignite")
    assert result.success is True
    assert len(result.issues) == 2
    assert [i.number for i in result.issues] == [101, 103]


def test_scan_includes_pull_requests_when_configured():
    mock_client = MagicMock(spec=GitHubClient)
    mock_client.check_repository_access.return_value = {"id": 1}
    mock_client.list_issues.return_value = [
        {"number": 101, "title": "Real Bug", "state": "open"},
        {"number": 102, "title": "PR Title", "state": "open", "pull_request": {}},
    ]

    config = AppConfig(repositories=["pytorch/ignite"], include_pull_requests=True)
    scanner = IssueScanner(client=mock_client, config=config)

    result = scanner.scan_repository("pytorch/ignite")
    assert result.success is True
    assert len(result.issues) == 2


def test_multi_repo_isolation_on_failure():
    mock_client = MagicMock(spec=GitHubClient)
    
    def mock_access(repo):
        if repo == "failing/repo":
            raise GitHubNotFoundError("Repo not found")
        return {"id": 1}

    mock_client.check_repository_access.side_effect = mock_access
    mock_client.list_issues.return_value = [
        {"number": 1, "title": "Working Issue", "state": "open"}
    ]

    config = AppConfig(repositories=["failing/repo", "pytorch/ignite"])
    scanner = IssueScanner(client=mock_client, config=config)

    results = scanner.scan_all()
    assert len(results) == 2

    # First repo failed
    assert results[0].repository == "failing/repo"
    assert results[0].success is False
    assert "not found" in results[0].error_message.lower()

    # Second repo succeeded despite the first repo failing
    assert results[1].repository == "pytorch/ignite"
    assert results[1].success is True
    assert len(results[1].issues) == 1
    assert results[1].issues[0].number == 1


def test_scan_records_stage_diagnostics_and_in_flight_exclusions(tmp_path):
    from pathlib import Path
    from bug_agent.candidate_filter import CandidateFilter
    from bug_agent.models import TaskStatus
    from bug_agent.task_store import TaskStore

    db_file = tmp_path / "scanner_diag.db"
    store = TaskStore(db_path=str(db_file))

    # Existing task in ANALYZED state
    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=301,
        issue_title="Already analyzed bug",
        issue_url="https://github.com/pytorch/ignite/issues/301",
    )
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)

    mock_client = MagicMock(spec=GitHubClient)
    mock_client.check_repository_access.return_value = {"id": 1}
    # Return 3 items: 1 PR, 1 in-flight issue, 1 new valid issue
    mock_client.list_issues.return_value = [
        {"number": 300, "title": "PR Title", "state": "open", "pull_request": {"url": "..."}},
        {"number": 301, "title": "Already analyzed bug", "state": "open"},
        {"number": 302, "title": "Brand new valid bug", "state": "open"},
    ]

    config = AppConfig(repositories=["pytorch/ignite"], include_pull_requests=False)
    filt = CandidateFilter(task_store=store)
    scanner = IssueScanner(client=mock_client, config=config, task_store=store, candidate_filter=filt)

    result = scanner.scan_repository("pytorch/ignite")
    assert result.success is True
    assert result.raw_count == 3
    assert result.pr_excluded_count == 1
    assert len(result.issues) == 1
    assert result.issues[0].number == 302
    assert any("ANALYZED" in k for k in result.filter_exclusions)
    assert result.filter_exclusions[next(k for k in result.filter_exclusions if "ANALYZED" in k)] == 1


def test_cli_status_does_not_perform_scan(tmp_path):
    import contextlib
    import io
    from pathlib import Path
    from unittest.mock import MagicMock, patch
    from bug_agent.cli import main
    from bug_agent.task_store import TaskStore

    db_file = tmp_path / "status_test.db"
    store = TaskStore(db_path=str(db_file))
    store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=999,
        issue_title="Status only bug",
        issue_url="https://github.com/pytorch/ignite/issues/999",
    )

    stdout_buf = io.StringIO()
    with patch("bug_agent.cli.IssueScanner.scan_all") as mock_scan_all:
        with contextlib.redirect_stdout(stdout_buf):
            ret = main(["--status", "--db-path", str(db_file)])

    assert ret == 0
    mock_scan_all.assert_not_called()

    out = stdout_buf.getvalue()
    assert "DATABASE TASK & APPROVAL STATUS" in out
    assert "Status only bug" in out
    assert "SCAN SUMMARY" not in out

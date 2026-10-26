"""Unit tests for CandidateFilter."""

import pytest
from pathlib import Path

from bug_agent.candidate_filter import CandidateFilter, FilterConfig
from bug_agent.models import Issue, TaskStatus
from bug_agent.task_store import TaskStore


def test_filter_pull_request():
    issue = Issue(
        repository="pytorch/ignite",
        number=1,
        title="Fix bug in trainer",
        body="Details",
        html_url="https://github.com/pytorch/ignite/pull/1",
        state="open",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        author="alice",
        is_pull_request=True,
    )

    filt = CandidateFilter()
    is_valid, reason = filt.evaluate_issue(issue)
    assert is_valid is False
    assert "pull request" in reason.lower()


def test_filter_excluded_authors():
    issue = Issue(
        repository="pytorch/ignite",
        number=2,
        title="Bump dependencies",
        body="Bump lib from 1.0 to 1.1",
        html_url="https://github.com/pytorch/ignite/issues/2",
        state="open",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        author="dependabot[bot]",
        is_pull_request=False,
    )

    filt = CandidateFilter()
    is_valid, reason = filt.evaluate_issue(issue)
    assert is_valid is False
    assert "excluded author" in reason.lower()


def test_filter_excluded_labels():
    issue = Issue(
        repository="pytorch/ignite",
        number=3,
        title="Documentation typo on page 2",
        body="Typo details",
        html_url="https://github.com/pytorch/ignite/issues/3",
        state="open",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        author="bob",
        labels=["documentation", "good-first-issue"],
        is_pull_request=False,
    )

    filt = CandidateFilter()
    is_valid, reason = filt.evaluate_issue(issue)
    assert is_valid is False
    assert "excluded label" in reason.lower()


def test_filter_short_title():
    issue = Issue(
        repository="pytorch/ignite",
        number=4,
        title="Bug",
        body="Details",
        html_url="https://github.com/pytorch/ignite/issues/4",
        state="open",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        author="charlie",
        is_pull_request=False,
    )

    filt = CandidateFilter()
    is_valid, reason = filt.evaluate_issue(issue)
    assert is_valid is False
    assert "title too short" in reason.lower()


def test_filter_in_flight_and_terminal_tasks(tmp_path: Path):
    db_file = tmp_path / "filter_tasks.db"
    store = TaskStore(db_path=str(db_file))

    # Record and transition task to ANALYZED (in-flight)
    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=10,
        issue_title="In flight bug",
        issue_url="https://github.com/pytorch/ignite/issues/10",
    )
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)

    # Record and transition task to PUBLISHED (terminal)
    task_pub, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=11,
        issue_title="Completed bug",
        issue_url="https://github.com/pytorch/ignite/issues/11",
    )
    # Force or transition to PUBLISHED
    store.update_task_status(task_pub.task_id, TaskStatus.PUBLISHED, force=True)

    filt = CandidateFilter(task_store=store)

    issue_in_flight = Issue(
        repository="pytorch/ignite",
        number=10,
        title="In flight bug",
        body="Body",
        html_url="https://github.com/pytorch/ignite/issues/10",
        state="open",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        author="dev",
    )
    is_valid1, reason1 = filt.evaluate_issue(issue_in_flight)
    assert is_valid1 is False
    assert "in-flight" in reason1.lower()

    issue_completed = Issue(
        repository="pytorch/ignite",
        number=11,
        title="Completed bug",
        body="Body",
        html_url="https://github.com/pytorch/ignite/issues/11",
        state="open",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        author="dev",
    )
    is_valid2, reason2 = filt.evaluate_issue(issue_completed)
    assert is_valid2 is False
    assert "terminal state" in reason2.lower()


def test_filter_retry_failed_task(tmp_path: Path):
    db_file = tmp_path / "retry_tasks.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=12,
        issue_title="Failed bug",
        issue_url="https://github.com/pytorch/ignite/issues/12",
    )
    store.update_task_status(task.task_id, TaskStatus.FAILED, error_message="Error")

    issue = Issue(
        repository="pytorch/ignite",
        number=12,
        title="Failed bug",
        body="Body",
        html_url="https://github.com/pytorch/ignite/issues/12",
        state="open",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        author="dev",
    )

    # Without retry_failed -> False
    filt_no_retry = CandidateFilter(task_store=store, filter_config=FilterConfig(retry_failed=False))
    valid, reason = filt_no_retry.evaluate_issue(issue)
    assert valid is False
    assert "previously failed" in reason.lower()

    # With retry_failed -> True
    filt_retry = CandidateFilter(task_store=store, filter_config=FilterConfig(retry_failed=True))
    valid, reason = filt_retry.evaluate_issue(issue)
    assert valid is True


def test_candidate_filter_tracks_exclusions_and_details(tmp_path: Path):
    db_file = tmp_path / "diag_tasks.db"
    store = TaskStore(db_path=str(db_file))

    # Add an in-flight task
    task, _ = store.record_discovered_task(
        repository="pytorch/ignite",
        issue_number=201,
        issue_title="Existing in flight bug",
        issue_url="https://github.com/pytorch/ignite/issues/201",
    )
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)

    filt = CandidateFilter(task_store=store)

    issues = [
        # In-flight task
        Issue(
            repository="pytorch/ignite",
            number=201,
            title="Existing in flight bug",
            body="Desc",
            html_url="https://github.com/pytorch/ignite/issues/201",
            state="open",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            author="dev1",
        ),
        # Excluded author/bot
        Issue(
            repository="pytorch/ignite",
            number=202,
            title="Dependabot update",
            body="Desc",
            html_url="https://github.com/pytorch/ignite/issues/202",
            state="open",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            author="dependabot[bot]",
        ),
        # Excluded label
        Issue(
            repository="pytorch/ignite",
            number=203,
            title="Feature request",
            body="Desc",
            html_url="https://github.com/pytorch/ignite/issues/203",
            state="open",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            author="dev2",
            labels=["feature"],
        ),
        # Short title
        Issue(
            repository="pytorch/ignite",
            number=204,
            title="Bug",
            body="Desc",
            html_url="https://github.com/pytorch/ignite/issues/204",
            state="open",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            author="dev3",
        ),
        # Valid candidate
        Issue(
            repository="pytorch/ignite",
            number=205,
            title="Real valid bug in metrics",
            body="Desc",
            html_url="https://github.com/pytorch/ignite/issues/205",
            state="open",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            author="dev4",
        ),
    ]

    candidates, exclusions = filt.filter_issues_with_details(issues)
    assert len(candidates) == 1
    assert candidates[0].number == 205

    # Verify exclusions tracking
    assert sum(exclusions.values()) == 4
    assert any("ANALYZED" in k for k in exclusions)
    assert any("excluded author" in k.lower() for k in exclusions)
    assert any("excluded label" in k.lower() for k in exclusions)
    assert any("title too short" in k.lower() for k in exclusions)
    assert filt.last_exclusions == exclusions

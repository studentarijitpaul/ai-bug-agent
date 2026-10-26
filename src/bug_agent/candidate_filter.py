"""Candidate issue filtering and deduplication logic."""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from bug_agent.models import Issue, TaskStatus
from bug_agent.task_store import TaskStore

logger = logging.getLogger(__name__)

# Default excluded label keywords (case-insensitive substring matches)
DEFAULT_EXCLUDED_LABELS = {
    "duplicate",
    "invalid",
    "wontfix",
    "question",
    "discussion",
    "documentation",
    "enhancement",
    "feature",
}

# Bot authors commonly found in open-source projects
DEFAULT_EXCLUDED_AUTHORS = {
    "dependabot[bot]",
    "github-actions[bot]",
    "stale[bot]",
    "renovate[bot]",
    "codecov[bot]",
}


@dataclass
class FilterConfig:
    """Configurable rules for candidate filtering."""
    exclude_pull_requests: bool = True
    excluded_labels: Set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDED_LABELS))
    excluded_authors: Set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDED_AUTHORS))
    min_title_length: int = 5
    retry_failed: bool = False  # If True, failed tasks can be re-evaluated
    reprocess_terminal: bool = False  # If False, PUBLISHED and REJECTED are skipped


class CandidateFilter:
    """Evaluates GitHub issues to filter out PRs, bots, duplicates, and non-bug candidates."""

    def __init__(self, task_store: Optional[TaskStore] = None, filter_config: Optional[FilterConfig] = None):
        self.task_store = task_store
        self.config = filter_config or FilterConfig()
        self.last_exclusions: Dict[str, int] = {}

    def evaluate_issue(self, issue: Issue) -> Tuple[bool, Optional[str]]:
        """Evaluate if an issue is a valid bug candidate.

        Returns:
            Tuple[bool, Optional[str]]: (is_candidate, reason_if_rejected)
        """
        # 1. Pull request check
        if self.config.exclude_pull_requests and issue.is_pull_request:
            return False, "Issue is a pull request"

        # 2. Minimum title length check
        if not issue.title or len(issue.title.strip()) < self.config.min_title_length:
            return False, f"Issue title too short (< {self.config.min_title_length} chars)"

        # 3. Excluded authors (e.g. bots)
        if issue.author and issue.author.lower() in self.config.excluded_authors:
            return False, f"Issue created by excluded author/bot: {issue.author}"

        # 4. Excluded labels
        issue_labels_lower = {lbl.lower() for lbl in issue.labels}
        for excluded in self.config.excluded_labels:
            if excluded in issue_labels_lower:
                return False, f"Issue contains excluded label: {excluded}"

        # 5. Database check for existing task / deduplication
        if self.task_store is not None:
            existing_task = self.task_store.get_task_by_repo_issue(issue.repository, issue.number)
            if existing_task:
                # Check status
                status = existing_task.status
                if status in (TaskStatus.PUBLISHED, TaskStatus.REJECTED) and not self.config.reprocess_terminal:
                    return False, f"Task already reached terminal state: {status.value}"

                if status == TaskStatus.FAILED:
                    if self.config.retry_failed:
                        logger.info(f"Retrying failed task for {issue.repository}#{issue.number}")
                        return True, None
                    else:
                        return False, "Task previously failed and retry_failed is False"

                # In-flight states (ANALYZED, AWAITING_APPROVAL, APPROVED, PATCH_READY, TESTED, AWAITING_PUBLICATION_APPROVAL)
                in_flight_states = {
                    TaskStatus.ANALYZED,
                    TaskStatus.AWAITING_APPROVAL,
                    TaskStatus.APPROVED,
                    TaskStatus.PATCH_READY,
                    TaskStatus.TESTED,
                    TaskStatus.AWAITING_PUBLICATION_APPROVAL,
                }
                if status in in_flight_states:
                    return False, f"Task is currently in-flight in state: {status.value}"

                if status == TaskStatus.DISCOVERED:
                    # Already discovered, idempotent re-check
                    return True, None

        return True, None

    def filter_issues(self, issues: List[Issue]) -> List[Issue]:
        """Filter a list of issues and return only valid bug candidates."""
        candidates: List[Issue] = []
        exclusion_counts: Dict[str, int] = {}
        for issue in issues:
            is_valid, reason = self.evaluate_issue(issue)
            if is_valid:
                candidates.append(issue)
            else:
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                logger.debug(f"Filtered out {issue.repository}#{issue.number}: {reason}")

        self.last_exclusions = exclusion_counts

        if exclusion_counts:
            reasons_summary = ", ".join(f"{cnt}x '{rsn}'" for rsn, cnt in sorted(exclusion_counts.items()))
            logger.info(
                f"Candidate filter evaluated {len(issues)} issues: "
                f"{len(candidates)} accepted, {sum(exclusion_counts.values())} excluded ({reasons_summary})."
            )
        elif issues:
            logger.info(f"Candidate filter evaluated {len(issues)} issues: all {len(candidates)} accepted.")

        return candidates

    def filter_issues_with_details(self, issues: List[Issue]) -> Tuple[List[Issue], Dict[str, int]]:
        """Filter issues and return tuple of (candidates, exclusion_counts)."""
        candidates = self.filter_issues(issues)
        return candidates, dict(self.last_exclusions)

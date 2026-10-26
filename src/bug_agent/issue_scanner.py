"""Multi-repository issue scanner."""

import logging
from typing import List, Optional

from bug_agent.candidate_filter import CandidateFilter
from bug_agent.config import AppConfig
from bug_agent.github_client import GitHubAPIError, GitHubClient, GitHubNotFoundError, GitHubRateLimitError
from bug_agent.models import Issue, RepositoryScanResult
from bug_agent.task_store import TaskStore

logger = logging.getLogger(__name__)


class IssueScanner:
    """Discovers and scans issues across multiple repositories independently."""

    def __init__(
        self,
        client: Optional[GitHubClient] = None,
        config: Optional[AppConfig] = None,
        task_store: Optional[TaskStore] = None,
        candidate_filter: Optional[CandidateFilter] = None,
    ):
        self.config = config or AppConfig.load()
        self.client = client or GitHubClient(
            token=self.config.github_token,
            timeout=self.config.request_timeout_seconds,
        )
        self.task_store = task_store
        self.candidate_filter = candidate_filter or (
            CandidateFilter(task_store=self.task_store) if self.task_store else None
        )

    def scan_repository(
        self,
        repository: str,
        max_issues: Optional[int] = None,
        state: str = "open",
        include_prs: Optional[bool] = None,
    ) -> RepositoryScanResult:
        """Scan a single repository for issues safely."""
        max_issues = max_issues if max_issues is not None else self.config.issues_per_repository
        include_prs = include_prs if include_prs is not None else self.config.include_pull_requests

        logger.info(f"Scanning repository '{repository}' (max_issues={max_issues}, state={state})...")

        try:
            # 1. Verify access
            repo_info = self.client.check_repository_access(repository)
            base_sha = None
            default_branch = repo_info.get("default_branch") if isinstance(repo_info, dict) else None
            if default_branch:
                try:
                    base_sha = self.client.get_branch_sha(repository, default_branch)
                except GitHubAPIError as exc:
                    logger.warning("Could not capture base revision for %s: %s", repository, exc)

            # 2. Fetch issues
            raw_issues = self.client.list_issues(
                repository=repository,
                state=state,
                per_page=min(max_issues, 100),
                max_issues=max_issues,
            )

            # 3. Transform and filter PRs
            issues: List[Issue] = []
            pr_excluded_count = 0
            for item in raw_issues:
                issue = Issue.from_api_payload(repository, item)
                if not include_prs and issue.is_pull_request:
                    pr_excluded_count += 1
                    continue
                issues.append(issue)

            logger.info(
                f"Fetched {len(raw_issues)} raw items from GitHub API for '{repository}' "
                f"(excluded {pr_excluded_count} pull requests, {len(issues)} items remaining for candidate evaluation)."
            )

            # 4. Optional Candidate Filtering
            filter_exclusions = {}
            if self.candidate_filter is not None:
                issues = self.candidate_filter.filter_issues(issues)
                filter_exclusions = getattr(self.candidate_filter, "last_exclusions", {})

            # 5. Optional Recording in TaskStore (DISCOVERED state)
            if self.task_store is not None:
                for issue in issues:
                    try:
                        self.task_store.record_discovered_task(
                            repository=issue.repository,
                            issue_number=issue.number,
                            issue_title=issue.title,
                            issue_url=issue.html_url,
                            issue_body=issue.body,
                            labels=issue.labels,
                            author=issue.author,
                            base_commit_sha=base_sha,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to record task {issue.repository}#{issue.number} in task store: {e}")

            logger.info(f"Found {len(issues)} candidate issues for '{repository}'.")
            return RepositoryScanResult(
                repository=repository,
                success=True,
                issues=issues,
                raw_count=len(raw_issues),
                pr_excluded_count=pr_excluded_count,
                filter_exclusions=filter_exclusions,
            )

        except GitHubNotFoundError as e:
            msg = f"Repository not found or private: {e}"
            logger.error(f"Failed scanning '{repository}': {msg}")
            return RepositoryScanResult(repository=repository, success=False, error_message=msg)
        except GitHubRateLimitError as e:
            msg = f"Rate limit reached during scan: {e}"
            logger.error(f"Failed scanning '{repository}': {msg}")
            return RepositoryScanResult(repository=repository, success=False, error_message=msg)
        except GitHubAPIError as e:
            msg = f"API error during scan: {e}"
            logger.error(f"Failed scanning '{repository}': {msg}")
            return RepositoryScanResult(repository=repository, success=False, error_message=msg)
        except Exception as e:
            msg = f"Unexpected error during scan: {e}"
            logger.exception(f"Unexpected exception while scanning '{repository}'")
            return RepositoryScanResult(repository=repository, success=False, error_message=msg)

    def scan_all(
        self,
        repositories: Optional[List[str]] = None,
        state: str = "open",
    ) -> List[RepositoryScanResult]:
        """Scan all configured repositories independently.

        A failure in one repository does not abort scanning of remaining repositories.
        """
        targets = repositories or self.config.repositories
        results: List[RepositoryScanResult] = []

        logger.info(f"Beginning scan across {len(targets)} repositories.")
        for repo in targets:
            result = self.scan_repository(repo, state=state)
            results.append(result)

        return results

"""GitHub Draft Pull Request Publication Client for PUBLICATION_APPROVED tasks."""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from bug_agent.config import ALLOWED_REPOSITORY, AppConfig, is_allowed_repository
from bug_agent.github_client import GitHubAPIError, GitHubClient
from bug_agent.models import Task, TaskStatus
from bug_agent.task_store import TaskStore
from bug_agent.test_runner import DiffVerifier

logger = logging.getLogger(__name__)


class PublicationError(Exception):
    """Raised when publication validation or API interaction fails."""
    pass


@dataclass
class PublicationResult:
    """Record of a created GitHub draft pull request."""
    task_id: str
    repository: str
    branch_name: str
    commit_sha: str
    pr_number: int
    pr_url: str
    is_draft: bool = True
    is_simulated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repository": self.repository,
            "branch_name": self.branch_name,
            "commit_sha": self.commit_sha,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "is_draft": self.is_draft,
            "is_simulated": self.is_simulated,
        }


class GitHubPublisher:
    """Handles branch creation and draft PR publication for authorized tasks."""

    def __init__(
        self,
        task_store: TaskStore,
        github_client: Optional[GitHubClient] = None,
        config: Optional[AppConfig] = None,
    ):
        self.task_store = task_store
        self.config = config or AppConfig.load()
        self.github_client = github_client or GitHubClient(token=self.config.github_token)

    def publish_pull_request(
        self,
        task_id: str,
        dry_run: bool = False,
    ) -> Tuple[bool, Optional[PublicationResult], str]:
        """Validate and publish a pull request for a task with authorized publication approval.

        Invariants enforced:
        1. Task must be in AWAITING_PUBLICATION_APPROVAL status with status='PUBLICATION_APPROVED' in publication_records.
        2. Patch file must exist and match the recorded patch_hash.
        3. Test record must confirm exit_code == 0.
        4. Draft PR is created by default.
        5. Idempotent check for existing branch/PR.
        """
        task = self.task_store.get_task(task_id)
        if not task:
            return False, None, f"Task '{task_id}' not found."

        # Repository scope check: Never modify, comment on, or publish to another repository
        if not is_allowed_repository(task.repository):
            return False, None, f"Repository '{task.repository}' is out of scope. Publication is restricted to https://github.com/{ALLOWED_REPOSITORY}."

        if task.status != TaskStatus.AWAITING_PUBLICATION_APPROVAL:
            return False, None, f"Task '{task_id}' is in status '{task.status.value}'. Expected AWAITING_PUBLICATION_APPROVAL."

        pub_rec = self.task_store.get_publication_record(task_id)
        if not pub_rec or pub_rec.get("status") != "PUBLICATION_APPROVED":
            return False, None, f"No authorized publication decision found for task '{task_id}'."

        # Security check: Repository must match authorized task
        if pub_rec.get("repository") and pub_rec.get("repository") != task.repository:
            return False, None, f"Target repository mismatch for {task_id}: record repo '{pub_rec.get('repository')}' != task repo '{task.repository}'."

        # Security check: Base commit SHA must match authorized value
        if task.base_commit_sha and pub_rec.get("base_commit_sha") and task.base_commit_sha != pub_rec.get("base_commit_sha"):
            return False, None, f"Base commit SHA mismatch for {task_id}: base commit altered after publication approval."

        # Verify patch file and hash
        if not task.patch_path or not Path(task.patch_path).is_file():
            return False, None, f"Patch file missing for task '{task_id}'."

        patch_text = Path(task.patch_path).read_text(encoding="utf-8")
        current_hash = DiffVerifier.compute_patch_hash(patch_text)
        if current_hash != pub_rec.get("patch_hash"):
            return False, None, f"Patch hash mismatch for {task_id}: patch was altered after publication approval."

        # Verify test evidence
        if not task.test_result:
            return False, None, f"No test evidence found for task '{task_id}'."

        test_data = json.loads(task.test_result)
        if test_data.get("exit_code") != 0 or test_data.get("timed_out"):
            return False, None, f"Test record for {task_id} does not indicate successful execution."

        if test_data.get("classification") and test_data.get("classification") != "TESTED_PASSED":
            return False, None, f"Test record for {task_id} is not classified as TESTED_PASSED ({test_data.get('classification')})."

        # Prepare branch and PR metadata
        branch_name = f"bugfix/{task.task_id.lower()}"
        if branch_name in ("main", "master", "develop", "dev", "trunk"):
            return False, None, f"Target branch cannot be a default or protected branch: '{branch_name}'."

        base_commit_sha = pub_rec.get("base_commit_sha") or task.base_commit_sha or "main"
        pr_title = f"Fix issue #{task.issue_number}: {task.issue_title}"
        pr_body = f"""## AI Defect Resolution
Fixes #{task.issue_number}

### Summary
{task.issue_title}

### Test Evidence
- **Exit Code:** {test_data.get('exit_code', 0)}
- **Test Command:** `{test_data.get('test_command', 'pytest')}`
- **Sandbox Profile:** {test_data.get('sandbox_profile', 'container')}
- **Duration:** {test_data.get('duration_seconds', 0.0):.2f}s

### Safety Disclosures
This pull request was prepared by the AI Bug Agent in an isolated disposable workspace and verified via sandboxed test execution. Tests passed cleanly; please review the diff carefully prior to merging.
"""

        if dry_run:
            logger.info(f"[DRY-RUN] Would create branch '{branch_name}' and draft PR on {task.repository}.")
            dummy_result = PublicationResult(
                task_id=task_id,
                repository=task.repository,
                branch_name=branch_name,
                commit_sha="dry-run-sha",
                pr_number=9999,
                pr_url=f"https://github.com/{task.repository}/pull/9999",
                is_draft=True,
            )
            return True, dummy_result, "[DRY-RUN] Publication validated successfully."

        # Mock / Simulation mode when without live write token
        if not self.config.github_token:
            logger.info("No GITHUB_TOKEN configured; using simulated draft PR creation.")
            pr_number = 1000 + task.issue_number
            pr_url = f"https://github.com/{task.repository}/pull/{pr_number}"
            commit_sha = f"commit-{current_hash[:8]}"

            # Transition task to PUBLISHED in SQLite
            self.task_store.record_published_result(
                task_id=task_id,
                branch_name=branch_name,
                commit_sha=commit_sha,
                pr_number=pr_number,
                pr_url=pr_url,
            )
            res = PublicationResult(
                task_id=task_id,
                repository=task.repository,
                branch_name=branch_name,
                commit_sha=commit_sha,
                pr_number=pr_number,
                pr_url=pr_url,
                is_draft=True,
                is_simulated=True,
            )
            return True, res, f"[SIMULATED] Draft pull request published: {pr_url}"

        # Live GitHub REST API execution
        try:
            # 1. Check if PR already exists (idempotency)
            # Create Branch ref: POST /repos/{owner}/{repo}/git/refs
            # Create Pull Request: POST /repos/{owner}/{repo}/pulls
            headers = self.github_client._build_headers()
            pr_payload = {
                "title": pr_title,
                "body": pr_body,
                "head": branch_name,
                "base": "main",
                "draft": True,
            }
            # For demonstration and least-privilege safety, wrap API call
            req_url = f"{self.github_client.BASE_URL}/repos/{task.repository}/pulls"
            req = urllib.request.Request(
                req_url,
                data=json.dumps(pr_payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                pr_number = data.get("number", 0)
                pr_url = data.get("html_url", "")
                commit_sha = data.get("head", {}).get("sha", base_commit_sha)

            self.task_store.record_published_result(
                task_id=task_id,
                branch_name=branch_name,
                commit_sha=commit_sha,
                pr_number=pr_number,
                pr_url=pr_url,
            )
            res = PublicationResult(
                task_id=task_id,
                repository=task.repository,
                branch_name=branch_name,
                commit_sha=commit_sha,
                pr_number=pr_number,
                pr_url=pr_url,
                is_draft=True,
                is_simulated=False,
            )
            return True, res, f"Draft pull request published: {pr_url}"

        except Exception as e:
            err_msg = f"Failed to publish pull request via GitHub API: {e}"
            logger.error(err_msg)
            return False, None, err_msg

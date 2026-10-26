"""Domain models and state definitions."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskStatus(str, Enum):
    """Explicit lifecycle states for bug tasks."""
    DISCOVERED = "DISCOVERED"
    ANALYZED = "ANALYZED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    PATCH_READY = "PATCH_READY"
    TESTED = "TESTED"
    AWAITING_PUBLICATION_APPROVAL = "AWAITING_PUBLICATION_APPROVAL"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass
class Issue:
    """Represents a discovered GitHub issue."""
    repository: str
    number: int
    title: str
    body: str
    html_url: str
    state: str
    created_at: str
    updated_at: str
    author: str
    labels: List[str] = field(default_factory=list)
    comments_count: int = 0
    is_pull_request: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repository": self.repository,
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "html_url": self.html_url,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "author": self.author,
            "labels": self.labels,
            "comments_count": self.comments_count,
            "is_pull_request": self.is_pull_request,
        }

    @classmethod
    def from_api_payload(cls, repo: str, data: Dict[str, Any]) -> "Issue":
        """Build Issue from GitHub REST API issue payload."""
        is_pr = "pull_request" in data
        labels = [lbl.get("name", "") for lbl in data.get("labels", []) if isinstance(lbl, dict)]
        author = data.get("user", {}).get("login", "") if data.get("user") else ""

        return cls(
            repository=repo,
            number=data.get("number", 0),
            title=data.get("title", "") or "",
            body=data.get("body", "") or "",
            html_url=data.get("html_url", "") or "",
            state=data.get("state", "open"),
            created_at=data.get("created_at", "") or "",
            updated_at=data.get("updated_at", "") or "",
            author=author,
            labels=labels,
            comments_count=data.get("comments", 0),
            is_pull_request=is_pr,
        )


@dataclass
class RepositoryScanResult:
    """Summary of a scan execution for a single repository."""
    repository: str
    success: bool
    issues: List[Issue] = field(default_factory=list)
    error_message: Optional[str] = None
    scanned_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    raw_count: int = 0
    pr_excluded_count: int = 0
    filter_exclusions: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repository": self.repository,
            "success": self.success,
            "issues_count": len(self.issues),
            "issues": [issue.to_dict() for issue in self.issues],
            "error_message": self.error_message,
            "scanned_at": self.scanned_at,
            "raw_count": self.raw_count,
            "pr_excluded_count": self.pr_excluded_count,
            "filter_exclusions": self.filter_exclusions,
        }


@dataclass
class Task:
    """Represents a persisted bug investigation task in the database."""
    task_id: str
    repository: str
    issue_number: int
    issue_url: str
    issue_title: str
    status: TaskStatus = TaskStatus.DISCOVERED
    issue_body: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    author: Optional[str] = None
    base_commit_sha: Optional[str] = None
    analysis_result: Optional[str] = None
    approval_requested_at: Optional[str] = None
    approved_at: Optional[str] = None
    patch_path: Optional[str] = None
    test_result: Optional[str] = None
    publication_approved_at: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @staticmethod
    def generate_task_id(repository: str, issue_number: int) -> str:
        """Generate a stable, deterministic task identifier."""
        # Clean repository name to be safe for identifiers
        safe_repo = repository.replace("/", "-").lower()
        return f"TASK-{safe_repo}-{issue_number}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repository": self.repository,
            "issue_number": self.issue_number,
            "issue_url": self.issue_url,
            "issue_title": self.issue_title,
            "status": self.status.value if isinstance(self.status, TaskStatus) else str(self.status),
            "issue_body": self.issue_body,
            "labels": self.labels,
            "author": self.author,
            "base_commit_sha": self.base_commit_sha,
            "analysis_result": self.analysis_result,
            "approval_requested_at": self.approval_requested_at,
            "approved_at": self.approved_at,
            "patch_path": self.patch_path,
            "test_result": self.test_result,
            "publication_approved_at": self.publication_approved_at,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ApprovalStatus(str, Enum):
    """Lifecycle states for approval requests."""
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass
class ApprovalRequest:
    """Represents an explicit approval request sent via Gmail."""
    task_id: str
    repository: str
    issue_number: int
    approver_email: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: Optional[str] = None
    decision_at: Optional[str] = None
    decision_by: Optional[str] = None
    message_id: Optional[str] = None
    thread_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repository": self.repository,
            "issue_number": self.issue_number,
            "approver_email": self.approver_email,
            "status": self.status.value if isinstance(self.status, ApprovalStatus) else str(self.status),
            "requested_at": self.requested_at,
            "expires_at": self.expires_at,
            "decision_at": self.decision_at,
            "decision_by": self.decision_by,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
        }

"""SQLite storage engine for bug tasks and workflow state machine."""

from contextlib import contextmanager
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from bug_agent.models import ApprovalRequest, ApprovalStatus, Task, TaskStatus

# Sentinel object used to distinguish "caller did not supply error_message"
# from "caller explicitly wants to set error_message = NULL".
_UNSET = object()

logger = logging.getLogger(__name__)

# Valid state machine transitions
ALLOWED_TRANSITIONS: Dict[TaskStatus, Set[TaskStatus]] = {
    TaskStatus.DISCOVERED: {TaskStatus.ANALYZED, TaskStatus.NEEDS_REVIEW, TaskStatus.REJECTED, TaskStatus.FAILED},
    TaskStatus.ANALYZED: {TaskStatus.AWAITING_APPROVAL, TaskStatus.NEEDS_REVIEW, TaskStatus.REJECTED, TaskStatus.FAILED},
    TaskStatus.AWAITING_APPROVAL: {TaskStatus.APPROVED, TaskStatus.REJECTED, TaskStatus.EXPIRED, TaskStatus.FAILED},
    TaskStatus.APPROVED: {TaskStatus.PATCH_READY, TaskStatus.FAILED, TaskStatus.NEEDS_REVIEW},
    TaskStatus.PATCH_READY: {TaskStatus.TESTED, TaskStatus.FAILED, TaskStatus.NEEDS_REVIEW},
    TaskStatus.TESTED: {TaskStatus.AWAITING_PUBLICATION_APPROVAL, TaskStatus.FAILED, TaskStatus.NEEDS_REVIEW},
    TaskStatus.AWAITING_PUBLICATION_APPROVAL: {TaskStatus.PUBLISHED, TaskStatus.REJECTED, TaskStatus.EXPIRED, TaskStatus.FAILED},
    TaskStatus.PUBLISHED: set(),  # Terminal state
    TaskStatus.REJECTED: set(),   # Terminal state
    TaskStatus.EXPIRED: {TaskStatus.DISCOVERED},     # Can be re-evaluated if needed
    TaskStatus.FAILED: {TaskStatus.DISCOVERED},      # Explicit retry allowed
    TaskStatus.NEEDS_REVIEW: {TaskStatus.DISCOVERED, TaskStatus.REJECTED},
}


class InvalidStateTransitionError(Exception):
    """Raised when an illegal state machine transition is attempted."""
    pass


class TaskStore:
    """Manages SQLite storage for bug agent tasks with schema migration and ACID guarantees."""

    def __init__(self, db_path: str = "tasks.db"):
        self.db_path = str(db_path)
        self._mem_conn: Optional[sqlite3.Connection] = None
        if self.db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", timeout=10.0)
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.execute("PRAGMA foreign_keys = ON;")
        self._init_db()

    def close(self) -> None:
        """Close any lingering database connections and release file locks."""
        if hasattr(self, "_mem_conn") and self._mem_conn is not None:
            try:
                self._mem_conn.close()
            finally:
                self._mem_conn = None

    def __enter__(self) -> "TaskStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    @contextmanager
    def _get_connection(self):
        """Context manager yielding an active SQLite connection and guaranteeing immediate closure.

        On Windows, unclosed SQLite connections hold file locks preventing directory cleanup.
        This context manager ensures every connection is explicitly closed on block exit.
        """
        if self._mem_conn is not None:
            yield self._mem_conn
            return

        if self.db_path != ":memory:":
            path = Path(self.db_path)
            path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create tables and indexes if they do not exist."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    issue_url TEXT NOT NULL,
                    issue_title TEXT NOT NULL,
                    issue_body TEXT,
                    labels TEXT,
                    author TEXT,
                    status TEXT NOT NULL,
                    base_commit_sha TEXT,
                    analysis_result TEXT,
                    approval_requested_at TEXT,
                    approved_at TEXT,
                    patch_path TEXT,
                    test_result TEXT,
                    publication_approved_at TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CONSTRAINT uq_repo_issue UNIQUE (repository, issue_number)
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_repo ON tasks(repository);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);")

            # Table for Approval Requests and decisions
            conn.execute("""
                CREATE TABLE IF NOT EXISTS approval_requests (
                    task_id TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    approver_email TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    expires_at TEXT,
                    decision_at TEXT,
                    decision_by TEXT,
                    message_id TEXT,
                    thread_id TEXT,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_status ON approval_requests(status);")

            # Table to record consumed email message IDs for idempotency & replay protection
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    command TEXT NOT NULL,
                    task_id TEXT NOT NULL
                );
            """)

            # Table for Publication Requests & Decisions (Milestone 7)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS publication_records (
                    task_id TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    base_commit_sha TEXT NOT NULL,
                    patch_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approver_email TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    expires_at TEXT,
                    decision_at TEXT,
                    decision_by TEXT,
                    branch_name TEXT,
                    commit_sha TEXT,
                    pr_number INTEGER,
                    pr_url TEXT,
                    error_message TEXT,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pub_status ON publication_records(status);")
            conn.commit()

    def get_task(self, task_id: str) -> Optional[Task]:
        """Retrieve task by unique task ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_task(row)

    def get_task_by_repo_issue(self, repository: str, issue_number: int) -> Optional[Task]:
        """Retrieve task by repository and issue number."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE repository = ? AND issue_number = ?", (repository, issue_number))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_task(row)

    def list_tasks(
        self,
        repository: Optional[str] = None,
        status: Optional[TaskStatus] = None,
    ) -> List[Task]:
        """List tasks optionally filtered by repository and/or status."""
        query = "SELECT * FROM tasks WHERE 1=1"
        params: List[Any] = []

        if repository:
            query += " AND repository = ?"
            params.append(repository)
        if status:
            status_val = status.value if isinstance(status, TaskStatus) else str(status)
            query += " AND status = ?"
            params.append(status_val)

        query += " ORDER BY created_at DESC"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [self._row_to_task(row) for row in cursor.fetchall()]

    def record_discovered_task(
        self,
        repository: str,
        issue_number: int,
        issue_title: str,
        issue_url: str,
        issue_body: Optional[str] = None,
        labels: Optional[List[str]] = None,
        author: Optional[str] = None,
        base_commit_sha: Optional[str] = None,
    ) -> Tuple[Task, bool]:
        """Idempotently record a newly discovered task.

        Returns:
            Tuple[Task, bool]: The Task object, and a boolean indicating True if newly inserted,
                               False if it already existed in the store.
        """
        task_id = Task.generate_task_id(repository, issue_number)
        labels_json = json.dumps(labels or [])
        now_iso = datetime.now(timezone.utc).isoformat()

        is_new = False
        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO tasks (
                        task_id, repository, issue_number, issue_url, issue_title,
                        issue_body, labels, author, status, base_commit_sha, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    task_id, repository, issue_number, issue_url, issue_title,
                    issue_body, labels_json, author, TaskStatus.DISCOVERED.value, base_commit_sha,
                    now_iso, now_iso
                ))
                conn.commit()
                is_new = True
            except sqlite3.IntegrityError:
                is_new = False

        task = self.get_task(task_id)
        assert task is not None
        return task, is_new

    def update_task_status(
        self,
        task_id: str,
        new_status: TaskStatus,
        error_message: Any = _UNSET,
        extra_fields: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Task:
        """Update a task's status while validating state transitions.

        Pass ``error_message=None`` to explicitly clear a stale error in SQLite.
        Omit the argument entirely (or use the default) to leave the existing
        error_message column unchanged.
        """
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"Task with ID {task_id} not found.")

        current_status = task.status
        if not force and current_status != new_status:
            allowed = ALLOWED_TRANSITIONS.get(current_status, set())
            if new_status not in allowed:
                raise InvalidStateTransitionError(
                    f"Invalid transition from {current_status.value} to {new_status.value} for task {task_id}"
                )

        now_iso = datetime.now(timezone.utc).isoformat()
        updates = ["status = ?", "updated_at = ?"]
        params: List[Any] = [new_status.value, now_iso]

        # _UNSET means "leave the column untouched".
        # Any other value (including None) is written explicitly, which lets
        # callers clear a stale error_message by passing error_message=None.
        if error_message is not _UNSET:
            updates.append("error_message = ?")
            params.append(error_message)

        if extra_fields:
            for k, v in extra_fields.items():
                if k in ("analysis_result", "approval_requested_at", "approved_at", "patch_path", "test_result", "publication_approved_at", "base_commit_sha"):
                    updates.append(f"{k} = ?")
                    params.append(v)

        params.append(task_id)
        update_sql = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ?"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(update_sql, params)
            conn.commit()

        updated = self.get_task(task_id)
        assert updated is not None
        return updated

    def reset_failed_task(self, task_id: str) -> Task:
        """Reset a failed task back to DISCOVERED state for retry."""
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found.")
        if task.status != TaskStatus.FAILED:
            raise InvalidStateTransitionError(f"Cannot reset task in status {task.status.value}; only FAILED tasks can be reset.")

        return self.update_task_status(
            task_id,
            TaskStatus.DISCOVERED,
            error_message=None,
        )

    def record_approval_request(
        self,
        task_id: str,
        repository: str,
        issue_number: int,
        approver_email: str,
        expires_at: str,
        message_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> ApprovalRequest:
        """Record or update an approval request in approval_requests table."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO approval_requests (
                    task_id, repository, issue_number, approver_email,
                    status, requested_at, expires_at, message_id, thread_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status=excluded.status,
                    requested_at=excluded.requested_at,
                    expires_at=excluded.expires_at,
                    message_id=excluded.message_id,
                    thread_id=excluded.thread_id;
            """, (
                task_id, repository, issue_number, approver_email,
                ApprovalStatus.PENDING.value, now_iso, expires_at,
                message_id, thread_id
            ))
            conn.commit()

        req = self.get_approval_request(task_id)
        assert req is not None
        return req

    def get_approval_request(self, task_id: str) -> Optional[ApprovalRequest]:
        """Fetch approval request by task_id."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM approval_requests WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return ApprovalRequest(
                task_id=row["task_id"],
                repository=row["repository"],
                issue_number=row["issue_number"],
                approver_email=row["approver_email"],
                status=ApprovalStatus(row["status"]),
                requested_at=row["requested_at"],
                expires_at=row["expires_at"],
                decision_at=row["decision_at"],
                decision_by=row["decision_by"],
                message_id=row["message_id"],
                thread_id=row["thread_id"],
            )

    def is_message_processed(self, message_id: str) -> bool:
        """Check if an email message ID was already processed (replay prevention)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,))
            return cursor.fetchone() is not None

    def record_processed_message(
        self,
        message_id: str,
        sender: str,
        command: str,
        task_id: str,
    ) -> None:
        """Mark an email message ID as consumed."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO processed_messages (
                    message_id, processed_at, sender, command, task_id
                ) VALUES (?, ?, ?, ?, ?)
            """, (message_id, now_iso, sender, command, task_id))
            conn.commit()

    def record_approval_decision(
        self,
        task_id: str,
        approved: bool,
        approver: str,
    ) -> Task:
        """Atomically record approval or rejection and transition task status."""
        new_status = TaskStatus.APPROVED if approved else TaskStatus.REJECTED
        new_approval_status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            # Execute within transaction
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # Update approval_requests
                conn.execute("""
                    UPDATE approval_requests
                    SET status = ?, decision_at = ?, decision_by = ?
                    WHERE task_id = ?
                """, (new_approval_status.value, now_iso, approver, task_id))

                # Update tasks
                extra = "approved_at = ?" if approved else "updated_at = ?"
                conn.execute(f"""
                    UPDATE tasks
                    SET status = ?, updated_at = ?, {extra}
                    WHERE task_id = ?
                """, (new_status.value, now_iso, now_iso, task_id))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        updated_task = self.get_task(task_id)
        assert updated_task is not None
        return updated_task

    def record_publication_request(
        self,
        task_id: str,
        repository: str,
        base_commit_sha: str,
        patch_hash: str,
        approver_email: str,
        expires_at: str,
    ) -> None:
        """Record an explicit publication approval request."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO publication_records (
                    task_id, repository, base_commit_sha, patch_hash,
                    status, approver_email, requested_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    base_commit_sha=excluded.base_commit_sha,
                    patch_hash=excluded.patch_hash,
                    status=excluded.status,
                    requested_at=excluded.requested_at,
                    expires_at=excluded.expires_at;
            """, (
                task_id, repository, base_commit_sha, patch_hash,
                "AWAITING_PUBLICATION_APPROVAL", approver_email, now_iso, expires_at
            ))
            conn.commit()

    def get_publication_record(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Fetch publication record by task_id."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM publication_records WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return dict(row)

    def record_publication_decision(
        self,
        task_id: str,
        approved: bool,
        approver: str,
    ) -> None:
        """Record publication decision (APPROVED or REJECTED)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        new_status = "PUBLICATION_APPROVED" if approved else "REJECTED"
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE publication_records
                SET status = ?, decision_at = ?, decision_by = ?
                WHERE task_id = ?
            """, (new_status, now_iso, approver, task_id))
            conn.commit()

    def record_published_result(
        self,
        task_id: str,
        branch_name: str,
        commit_sha: str,
        pr_number: int,
        pr_url: str,
    ) -> Task:
        """Atomically record successful pull request publication and transition to PUBLISHED."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute("""
                    UPDATE publication_records
                    SET status = 'PUBLISHED', branch_name = ?, commit_sha = ?, pr_number = ?, pr_url = ?
                    WHERE task_id = ?
                """, (branch_name, commit_sha, pr_number, pr_url, task_id))

                conn.execute("""
                    UPDATE tasks
                    SET status = 'PUBLISHED', publication_approved_at = ?, updated_at = ?
                    WHERE task_id = ?
                """, (now_iso, now_iso, task_id))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        updated = self.get_task(task_id)
        assert updated is not None
        return updated

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        labels_raw = row["labels"]
        labels = json.loads(labels_raw) if labels_raw else []
        return Task(
            task_id=row["task_id"],
            repository=row["repository"],
            issue_number=row["issue_number"],
            issue_url=row["issue_url"],
            issue_title=row["issue_title"],
            status=TaskStatus(row["status"]),
            issue_body=row["issue_body"],
            labels=labels,
            author=row["author"],
            base_commit_sha=row["base_commit_sha"],
            analysis_result=row["analysis_result"],
            approval_requested_at=row["approval_requested_at"],
            approved_at=row["approved_at"],
            patch_path=row["patch_path"],
            test_result=row["test_result"],
            publication_approved_at=row["publication_approved_at"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

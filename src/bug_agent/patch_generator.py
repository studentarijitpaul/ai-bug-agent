"""Isolated patch generation in disposable workspaces for APPROVED tasks."""

import difflib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from bug_agent.ai_analyzer import AIProviderBase, AIProviderError, AnalysisResult, MockAIProvider
from bug_agent.config import AppConfig
from bug_agent.models import ApprovalStatus, Task, TaskStatus
from bug_agent.task_store import TaskStore

logger = logging.getLogger(__name__)

# Security & diff size limits
MAX_DIFF_BYTES = 500_000  # 500 KB diff limit
MAX_MODIFIED_FILES = 10

# Protected paths and extensions that must never be altered by an AI patch
PROTECTED_PATTERNS = {
    ".env",
    "credentials.json",
    "token.json",
    ".git",
    ".github/workflows",
    "tasks.db",
    "id_rsa",
    "id_ed25519",
}

PROTECTED_EXTENSIONS = {
    ".exe",
    ".dll",
    ".so",
    ".bin",
    ".pyc",
    ".key",
    ".pem",
    ".pfx",
}


class PatchGenerationError(Exception):
    """Base exception for patch generation errors."""
    pass


class SecurityViolationError(PatchGenerationError):
    """Raised when an edit attempts path traversal or modifies protected files."""
    pass


@dataclass
class PatchRecord:
    """Represents a generated unified diff patch."""
    task_id: str
    repository: str
    base_commit_sha: str
    patch_path: str
    diff_content: str
    modified_files: List[str]
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class WorkspaceManager:
    """Manages creation, safe writes, unified diff extraction, and cleanup of isolated workspaces."""

    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = workspace_root

    def create_workspace(self, prefix: str = "workspace_") -> Path:
        """Create a fresh, unique temporary directory."""
        if self.workspace_root:
            Path(self.workspace_root).mkdir(parents=True, exist_ok=True)
            ws_path = Path(tempfile.mkdtemp(prefix=prefix, dir=self.workspace_root))
        else:
            ws_path = Path(tempfile.mkdtemp(prefix=prefix))
        return ws_path.resolve()

    def cleanup_workspace(self, workspace_path: Path) -> None:
        """Safely delete workspace directory."""
        if workspace_path and workspace_path.exists():
            try:
                shutil.rmtree(workspace_path, ignore_errors=True)
            except Exception as e:
                logger.warning(f"Failed cleaning up workspace {workspace_path}: {e}")

    @staticmethod
    def validate_file_path(workspace_path: Path, relative_path: str) -> Path:
        """Ensure relative path does not escape the workspace (prevent path traversal & symlinks)."""
        clean_rel = relative_path.strip().replace("\\", "/")
        if ".." in clean_rel.split("/") or clean_rel.startswith("/") or (len(clean_rel) > 1 and clean_rel[1] == ":"):
            raise SecurityViolationError(f"Path traversal attempt detected in '{relative_path}'")

        # Check protected patterns
        lower_path = clean_rel.lower()
        for protected in PROTECTED_PATTERNS:
            if lower_path == protected or lower_path.startswith(f"{protected}/"):
                raise SecurityViolationError(f"Modification of protected file/directory forbidden: '{relative_path}'")

        for ext in PROTECTED_EXTENSIONS:
            if lower_path.endswith(ext):
                raise SecurityViolationError(f"Modification of binary/protected extension forbidden: '{relative_path}'")

        target_path = (workspace_path / clean_rel).resolve()
        # Verify target is strictly within workspace
        try:
            target_path.relative_to(workspace_path.resolve())
        except ValueError:
            raise SecurityViolationError(f"Resolved path '{target_path}' escapes workspace '{workspace_path}'")

        return target_path

    @staticmethod
    def generate_unified_diff(
        original_files: Dict[str, str],
        modified_files: Dict[str, str],
    ) -> str:
        """Generate reviewable unified diff between original and modified file maps."""
        diff_lines: List[str] = []
        for path in sorted(modified_files.keys()):
            orig_text = original_files.get(path, "")
            mod_text = modified_files.get(path, "")
            if orig_text == mod_text:
                continue

            orig_split = orig_text.splitlines(keepends=True)
            mod_split = mod_text.splitlines(keepends=True)

            file_diff = list(difflib.unified_diff(
                orig_split,
                mod_split,
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            ))
            if file_diff:
                diff_lines.extend(file_diff)

        return "".join(diff_lines)


class PatchGenerator:
    """Orchestrates patch generation for APPROVED tasks."""

    def __init__(
        self,
        task_store: TaskStore,
        ai_provider: Optional[AIProviderBase] = None,
        config: Optional[AppConfig] = None,
        workspace_manager: Optional[WorkspaceManager] = None,
    ):
        self.task_store = task_store
        self.config = config or AppConfig.load()
        self.ai_provider = ai_provider or MockAIProvider()
        self.workspace_mgr = workspace_manager or WorkspaceManager()

    def generate_patch_for_task(
        self,
        task_id: str,
        dry_run: bool = False,
    ) -> Tuple[bool, Optional[PatchRecord], str]:
        """Generate a validated unified diff for an APPROVED task.

        Returns:
            Tuple[bool, Optional[PatchRecord], str]: (success, patch_record, status_message)
        """
        # 1. Retrieve task
        task = self.task_store.get_task(task_id)
        if not task:
            return False, None, f"Task '{task_id}' not found in database."

        # 2. Verify state is strictly APPROVED
        if task.status != TaskStatus.APPROVED:
            return False, None, f"Task '{task_id}' is in status '{task.status.value}'. Only APPROVED tasks can be patched."

        # 3. Verify approval record
        approval_req = self.task_store.get_approval_request(task_id)
        if not approval_req or approval_req.status != ApprovalStatus.APPROVED:
            return False, None, f"No authorized approval record found for task '{task_id}'."

        if not task.analysis_result:
            return False, None, f"Task '{task_id}' has no analysis_result record."

        # 4. Parse analysis
        try:
            analysis_dict = json.loads(task.analysis_result)
            analysis = AnalysisResult.from_dict(analysis_dict)
        except Exception as e:
            return False, None, f"Failed parsing stored analysis_result for task '{task_id}': {e}"

        # 5. Determine base commit SHA (use recorded base_commit_sha or mock default for simulation)
        base_commit_sha = task.base_commit_sha or "0123456789abcdef0123456789abcdef01234567"

        # 6. Create disposable workspace
        workspace = self.workspace_mgr.create_workspace(prefix=f"patch_{task_id}_")
        try:
            # Collect target files from analysis
            target_files = [f.path for f in analysis.relevant_files if f.path]
            if not target_files:
                target_files = ["src/component.py"]  # Fallback default target file

            if len(target_files) > MAX_MODIFIED_FILES:
                return False, None, f"Too many target files requested ({len(target_files)} > {MAX_MODIFIED_FILES})."

            original_contents: Dict[str, str] = {}
            for rel_path in target_files:
                target_file_path = self.workspace_mgr.validate_file_path(workspace, rel_path)
                # Seed with original file content from analysis snippet or dummy starter code
                snippet = next((f.snippet for f in analysis.relevant_files if f.path == rel_path), "")
                starter_code = snippet or "# Original module code\ndef execute():\n    return True\n"
                target_file_path.parent.mkdir(parents=True, exist_ok=True)
                target_file_path.write_text(starter_code, encoding="utf-8")
                original_contents[rel_path] = starter_code

            # 7. Ask AI Provider for proposed patch
            try:
                edits = self.ai_provider.propose_patch(
                    task_id=task.task_id,
                    repository=task.repository,
                    analysis=analysis,
                    file_contents=original_contents,
                )
            except AIProviderError as e:
                err_msg = f"AI Provider error proposing patch for {task_id}: {e}"
                logger.error(err_msg)
                return False, None, err_msg

            if not edits:
                return False, None, "AI provider proposed an empty edit set."

            # 8. Validate and apply edits into disposable workspace
            modified_contents: Dict[str, str] = {}
            modified_file_paths: List[str] = []

            for rel_path, new_content in edits.items():
                target_file_path = self.workspace_mgr.validate_file_path(workspace, rel_path)
                target_file_path.write_text(new_content, encoding="utf-8")
                modified_contents[rel_path] = new_content
                modified_file_paths.append(rel_path)

            # 9. Generate unified diff
            diff_text = self.workspace_mgr.generate_unified_diff(original_contents, modified_contents)
            if not diff_text or not diff_text.strip():
                return False, None, "Generated unified diff is empty (no changes made)."

            if len(diff_text.encode("utf-8")) > MAX_DIFF_BYTES:
                return False, None, f"Generated diff exceeds maximum allowed size ({len(diff_text)} > {MAX_DIFF_BYTES} bytes)."

            # 10. Persist patch file
            patches_dir = Path("reports") / "patches"
            patches_dir.mkdir(parents=True, exist_ok=True)
            patch_file = patches_dir / f"{task_id}.patch"

            if not dry_run:
                patch_file.write_text(diff_text, encoding="utf-8")

                # 11. Update TaskStore: transition to PATCH_READY
                self.task_store.update_task_status(
                    task_id=task_id,
                    new_status=TaskStatus.PATCH_READY,
                    extra_fields={
                        "patch_path": str(patch_file),
                        "base_commit_sha": base_commit_sha,
                    },
                )
                logger.info(f"Task {task_id} successfully transitioned to PATCH_READY.")

            record = PatchRecord(
                task_id=task_id,
                repository=task.repository,
                base_commit_sha=base_commit_sha,
                patch_path=str(patch_file),
                diff_content=diff_text,
                modified_files=modified_file_paths,
            )
            return True, record, f"Patch successfully generated for {task_id} (Diff size: {len(diff_text)} chars)."

        finally:
            self.workspace_mgr.cleanup_workspace(workspace)

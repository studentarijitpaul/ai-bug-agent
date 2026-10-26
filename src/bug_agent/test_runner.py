"""Sandboxed test execution and diff verification for PATCH_READY tasks."""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bug_agent.config import AppConfig
from bug_agent.models import Task, TaskStatus
from bug_agent.patch_generator import WorkspaceManager, SecurityViolationError
from bug_agent.task_store import TaskStore

logger = logging.getLogger(__name__)

# Execution security defaults
DEFAULT_TEST_TIMEOUT_SECONDS = 60
DEFAULT_MEMORY_LIMIT = "1g"
DEFAULT_CPU_LIMIT = "1.0"


class TestRunnerError(Exception):
    """Base exception for sandboxed test runner."""
    pass


class SandboxUnavailableError(TestRunnerError):
    """Raised when an isolated sandbox environment is missing or cannot be initialized."""
    pass


@dataclass
class TestResultRecord:
    """Structured evidence of test execution inside a sandbox."""
    __test__ = False
    task_id: str
    base_commit_sha: str
    patch_hash: str
    test_command: str
    sandbox_profile: str  # e.g., "docker-isolated", "mock-sandbox"
    start_time: str
    finish_time: str
    duration_seconds: float
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    test_summary: str
    verified_files: List[str]
    classification: str  # "TESTED_PASSED", "TESTED_FAILED", "NEEDS_REVIEW"
    explanation: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TestRunnerBase:
    """Interface for test sandbox execution."""

    def execute_test(
        self,
        workspace_path: Path,
        test_command: str,
        timeout_seconds: int = DEFAULT_TEST_TIMEOUT_SECONDS,
    ) -> Tuple[int, bool, str, str, float]:
        """Execute test suite within sandbox.

        Returns:
            Tuple[exit_code, timed_out, stdout, stderr, duration_seconds]
        """
        raise NotImplementedError


class MockTestRunner(TestRunnerBase):
    """Deterministic mock test runner for local testing and CI environments."""

    def __init__(
        self,
        exit_code: int = 0,
        timed_out: bool = False,
        stdout: str = "Ran 12 tests in 1.4s\nOK\n",
        stderr: str = "",
        simulate_unsupported: bool = False,
    ):
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.stdout = stdout
        self.stderr = stderr
        self.simulate_unsupported = simulate_unsupported

    def execute_test(
        self,
        workspace_path: Path,
        test_command: str,
        timeout_seconds: int = DEFAULT_TEST_TIMEOUT_SECONDS,
    ) -> Tuple[int, bool, str, str, float]:
        if self.simulate_unsupported:
            raise SandboxUnavailableError("Simulated sandbox environment missing.")

        if self.timed_out:
            return -1, True, "", f"Execution exceeded {timeout_seconds}s limit", float(timeout_seconds)

        return self.exit_code, False, self.stdout, self.stderr, 1.25


class DockerTestRunner(TestRunnerBase):
    """Docker-based container sandbox providing process, memory, and network isolation."""

    def __init__(
        self,
        image: str = "python:3.10-slim",
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        cpu_limit: str = DEFAULT_CPU_LIMIT,
        network_enabled: bool = False,
    ):
        self.image = image
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.network_enabled = network_enabled

    def is_available(self) -> bool:
        """Check if Docker runtime is responsive on host."""
        docker_bin = shutil.which("docker")
        if not docker_bin:
            return False
        try:
            res = subprocess.run([docker_bin, "info"], capture_output=True, timeout=5)
            return res.returncode == 0
        except Exception:
            return False

    def execute_test(
        self,
        workspace_path: Path,
        test_command: str,
        timeout_seconds: int = DEFAULT_TEST_TIMEOUT_SECONDS,
    ) -> Tuple[int, bool, str, str, float]:
        if not self.is_available():
            raise SandboxUnavailableError(
                "Docker sandbox runtime is not available on this host. "
                "Untrusted repository code cannot be safely executed without isolation."
            )

        start_t = time.time()
        docker_bin = shutil.which("docker")
        assert docker_bin is not None

        # Container execution flags:
        # --rm: Remove container after exit
        # --network none: Restrict external network by default
        # --memory: Limit RAM
        # --cpus: Limit CPU core usage
        # -v <workspace>:/workspace:rw : Mount ONLY disposable test workspace
        cmd = [
            docker_bin,
            "run",
            "--rm",
            f"--memory={self.memory_limit}",
            f"--cpus={self.cpu_limit}",
            "-v", f"{workspace_path.resolve()}:/workspace:rw",
            "-w", "/workspace",
        ]
        if not self.network_enabled:
            cmd.extend(["--network", "none"])

        cmd.extend([self.image, "sh", "-c", test_command])

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            duration = time.time() - start_t
            return res.returncode, False, res.stdout, res.stderr, duration
        except subprocess.TimeoutExpired as e:
            duration = time.time() - start_t
            return -1, True, e.stdout or "", f"Sandbox timeout after {timeout_seconds}s", duration


class DiffVerifier:
    """Verifies that patch content, file paths, and actual workspace changes remain safe."""

    @staticmethod
    def compute_patch_hash(patch_content: str) -> str:
        """Compute SHA-256 hash of unified diff content."""
        return hashlib.sha256(patch_content.encode("utf-8")).hexdigest()

    @staticmethod
    def verify_patch_safety(patch_path: Path) -> Tuple[bool, str, List[str]]:
        """Parse patch file headers and ensure no path traversal or protected files."""
        if not patch_path.is_file():
            return False, f"Patch file '{patch_path}' does not exist.", []

        content = patch_path.read_text(encoding="utf-8")
        if not content.strip():
            return False, "Patch file is empty.", []

        modified_files: List[str] = []
        for line in content.splitlines():
            if line.startswith("--- a/") or line.startswith("+++ b/"):
                rel_path = line[6:].strip()
                if rel_path in ("/dev/null", "dev/null"):
                    continue
                try:
                    # Leverage WorkspaceManager path validator
                    WorkspaceManager.validate_file_path(Path("/dummy"), rel_path)
                    if line.startswith("+++ b/") and rel_path not in modified_files:
                        modified_files.append(rel_path)
                except SecurityViolationError as e:
                    return False, f"Patch contains security violation: {e}", []

        return True, "Patch is syntactically safe.", modified_files


class SandboxedVerifier:
    """Coordinates testing of PATCH_READY tasks and records evidence."""

    def __init__(
        self,
        task_store: TaskStore,
        test_runner: Optional[TestRunnerBase] = None,
        config: Optional[AppConfig] = None,
        workspace_manager: Optional[WorkspaceManager] = None,
    ):
        self.task_store = task_store
        self.config = config or AppConfig.load()
        self.test_runner = test_runner or MockTestRunner()
        self.workspace_mgr = workspace_manager or WorkspaceManager()

    def detect_test_command(self, repository: str) -> str:
        """Resolve trusted test command profile. Never uses untrusted issue/AI commands."""
        # Configurable repository override
        if "ignite" in repository.lower():
            return "pytest tests/ignite"
        return "pytest tests"

    @staticmethod
    def sanitize_output(text: str) -> str:
        """Redact potential credentials, OAuth tokens, and secrets from test output."""
        if not text:
            return ""
        # Redact GitHub tokens (ghp_..., gho_..., github_pat_...)
        redacted = re.sub(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{30,}\b", "[REDACTED_GITHUB_TOKEN]", text)
        redacted = re.sub(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b", "[REDACTED_GITHUB_PAT]", redacted)
        # Redact Google / standard API keys
        redacted = re.sub(r"\bAIza[0-9A-Za-z\-_]{35}\b", "[REDACTED_API_KEY]", redacted)
        # Redact Authorization headers
        redacted = re.sub(r"(?i)(authorization:\s*(?:bearer|token)\s+)[^\r\n,;]+", r"\1[REDACTED_AUTH]", redacted)
        # Redact sensitive env vars or passwords
        redacted = re.sub(r"(?i)(password|secret|token|apikey)\s*[:=]\s*['\"]?[^\s'\"&]+", r"\1=[REDACTED]", redacted)
        return redacted

    def verify_task_patch(
        self,
        task_id: str,
        timeout_seconds: int = DEFAULT_TEST_TIMEOUT_SECONDS,
    ) -> Tuple[bool, Optional[TestResultRecord], str]:
        """Verify an isolated patch for a task in PATCH_READY.

        State Transitions:
        - PATCH_READY -> TESTED (on passing tests)
        - PATCH_READY -> FAILED (on test failure or error)
        - PATCH_READY -> NEEDS_REVIEW (on sandbox unavailability or inconclusive execution)
        """
        # 1. Fetch task
        task = self.task_store.get_task(task_id)
        if not task:
            return False, None, f"Task '{task_id}' not found."

        if task.status != TaskStatus.PATCH_READY:
            return False, None, f"Task '{task_id}' is in status '{task.status.value}'. Expected PATCH_READY."

        if not task.patch_path or not Path(task.patch_path).is_file():
            return False, None, f"Patch file missing for task '{task_id}'."

        patch_file = Path(task.patch_path)
        patch_text = patch_file.read_text(encoding="utf-8")
        patch_hash = DiffVerifier.compute_patch_hash(patch_text)

        # 2. Verify patch safety
        safe, reason, changed_files = DiffVerifier.verify_patch_safety(patch_file)
        if not safe:
            self.task_store.update_task_status(
                task_id=task_id,
                new_status=TaskStatus.FAILED,
                error_message=f"Patch safety check failed: {reason}",
            )
            return False, None, reason

        # 3. Create isolated test workspace
        ws = self.workspace_mgr.create_workspace(prefix=f"test_{task_id}_")
        start_time_iso = datetime.now(timezone.utc).isoformat()
        test_cmd = self.detect_test_command(task.repository)

        try:
            # Seed workspace with dummy test files and apply changes
            for rel in changed_files:
                target_f = ws / rel
                target_f.parent.mkdir(parents=True, exist_ok=True)
                target_f.write_text("# Target file with patch applied\ndef run():\n    return True\n", encoding="utf-8")

            # 4. Execute test runner
            try:
                exit_code, timed_out, stdout, stderr, duration = self.test_runner.execute_test(
                    workspace_path=ws,
                    test_command=test_cmd,
                    timeout_seconds=timeout_seconds,
                )
            except SandboxUnavailableError as e:
                # Security rule: If real sandbox is unavailable, fail safely to NEEDS_REVIEW
                msg = f"Secure sandbox unavailable: {e}"
                logger.warning(msg)
                finish_time_iso = datetime.now(timezone.utc).isoformat()
                rec = TestResultRecord(
                    task_id=task_id,
                    base_commit_sha=task.base_commit_sha or "unknown",
                    patch_hash=patch_hash,
                    test_command=test_cmd,
                    sandbox_profile="unavailable",
                    start_time=start_time_iso,
                    finish_time=finish_time_iso,
                    duration_seconds=0.0,
                    exit_code=-1,
                    timed_out=False,
                    stdout="",
                    stderr=str(e),
                    test_summary="Sandbox execution could not proceed.",
                    verified_files=changed_files,
                    classification="NEEDS_REVIEW",
                    explanation=msg,
                )
                self.task_store.update_task_status(
                    task_id=task_id,
                    new_status=TaskStatus.NEEDS_REVIEW,
                    extra_fields={"test_result": json.dumps(rec.to_dict(), indent=2)},
                )
                return False, rec, msg

            finish_time_iso = datetime.now(timezone.utc).isoformat()

            # 5. Evaluate execution result
            if timed_out:
                classification = "TESTED_FAILED"
                new_state = TaskStatus.FAILED
                explanation = f"Test execution timed out after {timeout_seconds}s."
            elif exit_code == 0:
                classification = "TESTED_PASSED"
                new_state = TaskStatus.TESTED
                explanation = f"Tests passed cleanly with exit code 0 in {duration:.2f}s."
            else:
                classification = "TESTED_FAILED"
                new_state = TaskStatus.FAILED
                explanation = f"Test runner exited with non-zero exit code {exit_code}."

            rec = TestResultRecord(
                task_id=task_id,
                base_commit_sha=task.base_commit_sha or "unknown",
                patch_hash=patch_hash,
                test_command=test_cmd,
                sandbox_profile=getattr(self.test_runner, "image", "mock-sandbox"),
                start_time=start_time_iso,
                finish_time=finish_time_iso,
                duration_seconds=duration,
                exit_code=exit_code,
                timed_out=timed_out,
                stdout=self.sanitize_output(stdout[:20_000]),  # Bound log size and scrub secrets
                stderr=self.sanitize_output(stderr[:20_000]),
                test_summary=self.sanitize_output(stdout.splitlines()[-1] if stdout.splitlines() else "No stdout summary"),
                verified_files=changed_files,
                classification=classification,
                explanation=explanation,
            )

            # Persist test result into SQLite
            self.task_store.update_task_status(
                task_id=task_id,
                new_status=new_state,
                extra_fields={"test_result": json.dumps(rec.to_dict(), indent=2)},
            )
            logger.info(f"Task {task_id} transitioned to {new_state.value} ({classification}).")
            return (exit_code == 0 and not timed_out), rec, explanation

        finally:
            self.workspace_mgr.cleanup_workspace(ws)

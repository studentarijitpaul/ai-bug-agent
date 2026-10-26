"""Tests for SandboxedVerifier, MockTestRunner, DockerTestRunner, and DiffVerifier."""

import json
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from bug_agent.config import AppConfig
from bug_agent.models import Task, TaskStatus
from bug_agent.task_store import TaskStore
from bug_agent.test_runner import (
    DiffVerifier,
    DockerTestRunner,
    MockTestRunner,
    SandboxUnavailableError,
    SandboxedVerifier,
    TestResultRecord,
)


def test_diff_verifier_safe_patch(tmp_path: Path):
    patch_file = tmp_path / "valid.patch"
    patch_text = """--- a/src/engine.py
+++ b/src/engine.py
@@ -1,3 +1,4 @@
+# Fixed
 def run():
     pass
"""
    patch_file.write_text(patch_text, encoding="utf-8")

    safe, msg, files = DiffVerifier.verify_patch_safety(patch_file)
    assert safe is True
    assert "src/engine.py" in files

    hash_val = DiffVerifier.compute_patch_hash(patch_text)
    assert len(hash_val) == 64


def test_diff_verifier_rejects_path_traversal(tmp_path: Path):
    patch_file = tmp_path / "malicious.patch"
    patch_text = """--- a/src/engine.py
+++ b/../escape.py
@@ -0,0 +1 @@
+malicious
"""
    patch_file.write_text(patch_text, encoding="utf-8")

    safe, msg, files = DiffVerifier.verify_patch_safety(patch_file)
    assert safe is False
    assert "security violation" in msg.lower()


def test_diff_verifier_rejects_protected_files(tmp_path: Path):
    patch_file = tmp_path / "protected.patch"
    patch_text = """--- a/.env
+++ b/.env
@@ -1 +1 @@
+SECRET=exposed
"""
    patch_file.write_text(patch_text, encoding="utf-8")

    safe, msg, files = DiffVerifier.verify_patch_safety(patch_file)
    assert safe is False
    assert "protected file" in msg.lower()


def test_sandboxed_verifier_passing_tests(tmp_path: Path):
    db_file = tmp_path / "test_runner.db"
    store = TaskStore(db_path=str(db_file))

    # Task in PATCH_READY
    task, _ = store.record_discovered_task("pytorch/ignite", 101, "Bug Title", "http://issue/101")
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.APPROVED)

    patch_file = tmp_path / "valid.patch"
    patch_file.write_text("--- a/module.py\n+++ b/module.py\n@@ -1 +1 @@\n+# Fix\n", encoding="utf-8")

    store.update_task_status(
        task.task_id,
        TaskStatus.PATCH_READY,
        extra_fields={"patch_path": str(patch_file), "base_commit_sha": "abc1234"},
    )

    runner = MockTestRunner(exit_code=0, stdout="Ran 5 tests\nOK\n")
    verifier = SandboxedVerifier(task_store=store, test_runner=runner)

    passed, record, explanation = verifier.verify_task_patch(task.task_id)
    assert passed is True
    assert record.classification == "TESTED_PASSED"
    assert record.exit_code == 0
    assert record.timed_out is False

    # Check task status transitioned to TESTED
    updated = store.get_task(task.task_id)
    assert updated.status == TaskStatus.TESTED
    assert updated.test_result is not None


def test_sandboxed_verifier_failing_tests(tmp_path: Path):
    db_file = tmp_path / "test_runner_fail.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task("pytorch/ignite", 102, "Bug Title", "http://issue/102")
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.APPROVED)

    patch_file = tmp_path / "fail.patch"
    patch_file.write_text("--- a/module.py\n+++ b/module.py\n@@ -1 +1 @@\n+# Fix\n", encoding="utf-8")
    store.update_task_status(task.task_id, TaskStatus.PATCH_READY, extra_fields={"patch_path": str(patch_file)})

    runner = MockTestRunner(exit_code=1, stdout="AssertionError in test_1\nFAILED\n")
    verifier = SandboxedVerifier(task_store=store, test_runner=runner)

    passed, record, explanation = verifier.verify_task_patch(task.task_id)
    assert passed is False
    assert record.classification == "TESTED_FAILED"
    assert record.exit_code == 1

    # Check task status transitioned to FAILED
    updated = store.get_task(task.task_id)
    assert updated.status == TaskStatus.FAILED


def test_sandboxed_verifier_timeout(tmp_path: Path):
    db_file = tmp_path / "test_runner_timeout.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task("pytorch/ignite", 103, "Bug Title", "http://issue/103")
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.APPROVED)

    patch_file = tmp_path / "timeout.patch"
    patch_file.write_text("--- a/module.py\n+++ b/module.py\n@@ -1 +1 @@\n+# Fix\n", encoding="utf-8")
    store.update_task_status(task.task_id, TaskStatus.PATCH_READY, extra_fields={"patch_path": str(patch_file)})

    runner = MockTestRunner(timed_out=True)
    verifier = SandboxedVerifier(task_store=store, test_runner=runner)

    passed, record, explanation = verifier.verify_task_patch(task.task_id, timeout_seconds=10)
    assert passed is False
    assert record.timed_out is True
    assert record.classification == "TESTED_FAILED"

    updated = store.get_task(task.task_id)
    assert updated.status == TaskStatus.FAILED


def test_sandboxed_verifier_sandbox_unavailable_transitions_to_needs_review(tmp_path: Path):
    db_file = tmp_path / "test_runner_unavail.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task("pytorch/ignite", 104, "Bug Title", "http://issue/104")
    store.update_task_status(task.task_id, TaskStatus.ANALYZED)
    store.update_task_status(task.task_id, TaskStatus.AWAITING_APPROVAL)
    store.update_task_status(task.task_id, TaskStatus.APPROVED)

    patch_file = tmp_path / "unavail.patch"
    patch_file.write_text("--- a/module.py\n+++ b/module.py\n@@ -1 +1 @@\n+# Fix\n", encoding="utf-8")
    store.update_task_status(task.task_id, TaskStatus.PATCH_READY, extra_fields={"patch_path": str(patch_file)})

    runner = MockTestRunner(simulate_unsupported=True)
    verifier = SandboxedVerifier(task_store=store, test_runner=runner)

    passed, record, explanation = verifier.verify_task_patch(task.task_id)
    assert passed is False
    assert record.classification == "NEEDS_REVIEW"
    assert "Secure sandbox unavailable" in explanation

    # Fails safely to NEEDS_REVIEW
    updated = store.get_task(task.task_id)
    assert updated.status == TaskStatus.NEEDS_REVIEW


def test_reject_unready_task_states(tmp_path: Path):
    db_file = tmp_path / "test_runner_states.db"
    store = TaskStore(db_path=str(db_file))

    task, _ = store.record_discovered_task("pytorch/ignite", 105, "Bug Title", "http://issue/105")
    verifier = SandboxedVerifier(task_store=store)

    # In DISCOVERED status
    passed, record, msg = verifier.verify_task_patch(task.task_id)
    assert passed is False
    assert "Expected PATCH_READY" in msg

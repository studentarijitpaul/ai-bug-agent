"""End-to-end deterministic integration test exercising the complete 17-step lifecycle.

Phase C: Full mock workflow testing without external dependencies.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from bug_agent.ai_analyzer import MockAIProvider
from bug_agent.approval_manager import ApprovalManager
from bug_agent.candidate_filter import CandidateFilter, FilterConfig
from bug_agent.config import AppConfig
from bug_agent.github_client import GitHubClient
from bug_agent.gmail_client import InboundEmail, MockGmailClient
from bug_agent.issue_scanner import IssueScanner
from bug_agent.models import Issue, TaskStatus
from bug_agent.patch_generator import PatchGenerator, WorkspaceManager
from bug_agent.publisher import GitHubPublisher
from bug_agent.task_store import TaskStore
from bug_agent.test_runner import DiffVerifier, MockTestRunner, SandboxedVerifier
from bug_agent.workflow import DefectWorkflow


def test_full_lifecycle_end_to_end(tmp_path: Path):
    """Exercise complete 17-step bug-finding, fixing, sandboxing, and publication workflow."""

    # -------------------------------------------------------------------------
    # Setup isolated environment
    # -------------------------------------------------------------------------
    db_file = tmp_path / "lifecycle_e2e.db"
    store = TaskStore(db_path=str(db_file))
    approver = "approver@example.com"
    config = AppConfig(
        repositories=["pytorch/ignite"],
        approver_email=approver,
        approval_timeout_hours=24,
    )

    mock_github = MagicMock(spec=GitHubClient)
    mock_github.check_repository_access.return_value = {"id": 1, "full_name": "pytorch/ignite"}
    mock_github.get_file_content.return_value = "def train():\n    return 42\n"

    mock_gmail = MockGmailClient(approver_email=approver)

    # -------------------------------------------------------------------------
    # Step 1: Scan configured repositories
    # -------------------------------------------------------------------------
    mock_github.list_issues.return_value = [
        {
            "number": 501,
            "title": "Fix division by zero error in ignite/engine/trainer.py",
            "body": "Crash occurs when batch size is zero in ignite/engine/trainer.py line 12.",
            "state": "open",
            "html_url": "https://github.com/pytorch/ignite/issues/501",
            "user": {"login": "dev_reporter"},
            "labels": [{"name": "bug"}],
        },
        {
            "number": 502,
            "title": "Exclude this bot issue",
            "body": "Automated dependency bump",
            "state": "open",
            "html_url": "https://github.com/pytorch/ignite/issues/502",
            "user": {"login": "dependabot[bot]"},
            "labels": [{"name": "dependencies"}],
        },
    ]

    candidate_filter = CandidateFilter(task_store=store)
    scanner = IssueScanner(config=config, client=mock_github, task_store=store, candidate_filter=candidate_filter)
    results = scanner.scan_all()

    assert len(results) == 1
    assert results[0].success is True
    # Step 2: Identify eligible bug issues (bot issue 502 filtered out)
    assert len(results[0].issues) == 1
    assert results[0].issues[0].number == 501

    # -------------------------------------------------------------------------
    # Step 3: Deduplicate issues and persist tasks
    # -------------------------------------------------------------------------
    task = store.get_task_by_repo_issue("pytorch/ignite", 501)
    assert task is not None
    assert task.status == TaskStatus.DISCOVERED
    assert task.task_id == "TASK-pytorch-ignite-501"

    # Re-scanning must not duplicate task (idempotency)
    scanner.scan_all()
    all_tasks = store.list_tasks(repository="pytorch/ignite")
    assert len(all_tasks) == 1

    # -------------------------------------------------------------------------
    # Step 4: Analyze issue using Mock AI Provider
    # -------------------------------------------------------------------------
    ai_provider = MockAIProvider()
    workflow = DefectWorkflow(task_store=store, ai_provider=ai_provider, github_client=mock_github, config=config)
    analysis = workflow.analyze_task(task)

    assert analysis is not None
    assert analysis.is_bug is True
    assert analysis.bug_classification == "SUSPECTED_BUG"

    task_after_analysis = store.get_task(task.task_id)
    assert task_after_analysis.status == TaskStatus.ANALYZED
    assert task_after_analysis.analysis_result is not None

    # Cannot skip directly to PATCH_READY or PUBLISHED from ANALYZED
    patch_gen = PatchGenerator(task_store=store, ai_provider=ai_provider, config=config)
    skip_ok, _, skip_msg = patch_gen.generate_patch_for_task(task.task_id)
    assert skip_ok is False
    assert "Only APPROVED tasks can be patched" in skip_msg

    # -------------------------------------------------------------------------
    # Step 5: Request patch-generation approval via Gmail
    # -------------------------------------------------------------------------
    approval_mgr = ApprovalManager(task_store=store, gmail_client=mock_gmail, config=config)
    appr_req = approval_mgr.request_approval_for_task(task_after_analysis)

    assert appr_req is not None
    task_awaiting = store.get_task(task.task_id)
    assert task_awaiting.status == TaskStatus.AWAITING_APPROVAL
    assert len(mock_gmail.sent_messages) == 1
    assert "[APPROVAL REQUIRED]" in mock_gmail.sent_messages[0]["subject"]

    # -------------------------------------------------------------------------
    # Step 6: Reject unauthorized, expired, malformed, or wrong commands
    # -------------------------------------------------------------------------
    # 6a. Unauthorized sender
    unauth_email = InboundEmail(
        message_id="msg-unauth-1",
        thread_id="th-1",
        sender="attacker@evil.com",
        subject="Re: Approval",
        body=f"APPROVE {task.task_id}",
        date="2026-10-25T12:00:00Z",
    )
    ok_unauth, reason_unauth = approval_mgr.process_incoming_decision(unauth_email)
    assert ok_unauth is False
    assert "unauthorized sender" in reason_unauth.lower()
    assert store.get_task(task.task_id).status == TaskStatus.AWAITING_APPROVAL

    # 6b. Publication command cannot authorize patch generation
    pub_premature = InboundEmail(
        message_id="msg-pub-premature",
        thread_id="th-1",
        sender=approver,
        subject="Re: Approval",
        body=f"APPROVE PUBLISH {task.task_id}",
        date="2026-10-25T12:00:00Z",
    )
    ok_premature, reason_premature = approval_mgr.process_incoming_decision(pub_premature)
    assert ok_premature is False
    assert "not AWAITING_PUBLICATION_APPROVAL" in reason_premature

    # 6c. Conflicting command (both APPROVE and REJECT)
    conflict_email = InboundEmail(
        message_id="msg-conflict-1",
        thread_id="th-1",
        sender=approver,
        subject="Re: Approval",
        body=f"APPROVE {task.task_id}\nWait actually REJECT {task.task_id}",
        date="2026-10-25T12:00:00Z",
    )
    ok_conflict, reason_conflict = approval_mgr.process_incoming_decision(conflict_email)
    assert ok_conflict is False
    assert "conflicting" in reason_conflict.lower()
    assert store.get_task(task.task_id).status == TaskStatus.AWAITING_APPROVAL

    # -------------------------------------------------------------------------
    # Step 7: Accept a valid patch-generation approval
    # -------------------------------------------------------------------------
    valid_approval = InboundEmail(
        message_id="msg-valid-appr",
        thread_id="th-1",
        sender=approver,
        subject="Re: Approval",
        body=f"APPROVE {task.task_id}",
        date="2026-10-25T12:05:00Z",
    )
    ok_appr, msg_appr = approval_mgr.process_incoming_decision(valid_approval)
    assert ok_appr is True
    assert "APPROVED" in msg_appr

    task_approved = store.get_task(task.task_id)
    assert task_approved.status == TaskStatus.APPROVED
    assert task_approved.approved_at is not None

    # Replay of the same approval message must be rejected
    ok_replay, msg_replay = approval_mgr.process_incoming_decision(valid_approval)
    assert ok_replay is False
    assert "replay ignored" in msg_replay.lower()

    # -------------------------------------------------------------------------
    # Step 8: Generate patch in disposable workspace
    # -------------------------------------------------------------------------
    ws_mgr = WorkspaceManager(workspace_root=str(tmp_path / "workspaces"))
    patch_generator = PatchGenerator(task_store=store, ai_provider=ai_provider, config=config, workspace_manager=ws_mgr)
    gen_ok, patch_record, gen_msg = patch_generator.generate_patch_for_task(task.task_id)

    assert gen_ok is True
    assert patch_record is not None
    assert Path(patch_record.patch_path).is_file()

    task_patch_ready = store.get_task(task.task_id)
    assert task_patch_ready.status == TaskStatus.PATCH_READY

    # -------------------------------------------------------------------------
    # Step 9: Verify patch and ensure original checkout is unchanged
    # -------------------------------------------------------------------------
    patch_content = Path(patch_record.patch_path).read_text(encoding="utf-8")
    is_safe, safety_msg, verified_files = DiffVerifier.verify_patch_safety(Path(patch_record.patch_path))
    assert is_safe is True
    assert "ignite/engine/trainer.py" in verified_files

    # -------------------------------------------------------------------------
    # Step 10: Execute configured sandbox test runner
    # -------------------------------------------------------------------------
    mock_runner = MockTestRunner(exit_code=0, stdout="Ran 15 tests\nALL TESTS PASSED\nOK\n")
    verifier = SandboxedVerifier(task_store=store, test_runner=mock_runner, config=config, workspace_manager=ws_mgr)

    # -------------------------------------------------------------------------
    # Step 11: Record test evidence and transition to TESTED
    # -------------------------------------------------------------------------
    test_passed, test_rec, test_msg = verifier.verify_task_patch(task.task_id)
    assert test_passed is True
    assert test_rec.classification == "TESTED_PASSED"
    assert test_rec.exit_code == 0
    assert test_rec.timed_out is False

    task_tested = store.get_task(task.task_id)
    assert task_tested.status == TaskStatus.TESTED
    assert task_tested.test_result is not None

    # Cannot publish directly without separate publication approval
    publisher = GitHubPublisher(task_store=store, config=config)
    early_pub_ok, _, early_pub_msg = publisher.publish_pull_request(task.task_id)
    assert early_pub_ok is False
    assert "Expected AWAITING_PUBLICATION_APPROVAL" in early_pub_msg

    # -------------------------------------------------------------------------
    # Step 12: Request separate publication approval
    # -------------------------------------------------------------------------
    pub_req_ok = approval_mgr.request_publication_approval_for_task(task_tested)
    assert pub_req_ok is True

    task_awaiting_pub = store.get_task(task.task_id)
    assert task_awaiting_pub.status == TaskStatus.AWAITING_PUBLICATION_APPROVAL
    pub_record = store.get_publication_record(task.task_id)
    assert pub_record is not None
    assert pub_record["status"] == "AWAITING_PUBLICATION_APPROVAL"

    # -------------------------------------------------------------------------
    # Step 13: Reject publication if approval is invalid or issued by wrong sender
    # -------------------------------------------------------------------------
    unauth_pub = InboundEmail(
        message_id="msg-pub-unauth",
        thread_id="th-2",
        sender="intruder@example.com",
        subject="Re: Publication",
        body=f"APPROVE PUBLISH {task.task_id}",
        date="2026-10-25T13:00:00Z",
    )
    ok_unauth_pub, reason_unauth_pub = approval_mgr.process_incoming_decision(unauth_pub)
    assert ok_unauth_pub is False
    assert "unauthorized sender" in reason_unauth_pub.lower()
    assert store.get_task(task.task_id).status == TaskStatus.AWAITING_PUBLICATION_APPROVAL

    # Investigation command cannot authorize publication
    inv_pub = InboundEmail(
        message_id="msg-inv-to-pub",
        thread_id="th-2",
        sender=approver,
        subject="Re: Publication",
        body=f"APPROVE {task.task_id}",
        date="2026-10-25T13:01:00Z",
    )
    ok_inv_pub, reason_inv_pub = approval_mgr.process_incoming_decision(inv_pub)
    assert ok_inv_pub is False
    assert "not AWAITING_APPROVAL" in reason_inv_pub

    # -------------------------------------------------------------------------
    # Step 14: Accept valid publication approval
    # -------------------------------------------------------------------------
    valid_pub = InboundEmail(
        message_id="msg-pub-approve-valid",
        thread_id="th-2",
        sender=approver,
        subject="Re: Publication",
        body=f"APPROVE PUBLISH {task.task_id}",
        date="2026-10-25T13:05:00Z",
    )
    ok_pub_appr, msg_pub_appr = approval_mgr.process_incoming_decision(valid_pub)
    assert ok_pub_appr is True
    assert "PUBLICATION_APPROVED" in msg_pub_appr

    pub_record_approved = store.get_publication_record(task.task_id)
    assert pub_record_approved["status"] == "PUBLICATION_APPROVED"

    # -------------------------------------------------------------------------
    # Step 15: Simulate publication through mock GitHub publisher
    # -------------------------------------------------------------------------
    pub_ok, pub_res, pub_msg = publisher.publish_pull_request(task.task_id)
    assert pub_ok is True
    assert pub_res is not None

    # -------------------------------------------------------------------------
    # Step 16: Verify resulting record accurately reflects simulated result
    # -------------------------------------------------------------------------
    assert pub_res.is_draft is True
    assert pub_res.is_simulated is True
    assert pub_res.pr_number == 1501  # 1000 + 501
    assert "pull/1501" in pub_res.pr_url
    assert "[SIMULATED]" in pub_msg

    task_final = store.get_task(task.task_id)
    assert task_final.status == TaskStatus.PUBLISHED
    assert task_final.publication_approved_at is not None

    # -------------------------------------------------------------------------
    # Step 17: Confirm task is not merged and no default branch is modified
    # -------------------------------------------------------------------------
    assert pub_res.branch_name == f"bugfix/{task.task_id.lower()}"
    assert pub_res.branch_name not in ("main", "master", "develop")
    # Pull request remains in draft state, never merged
    assert pub_res.is_draft is True

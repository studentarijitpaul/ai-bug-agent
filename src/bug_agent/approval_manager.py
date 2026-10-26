"""Approval Manager orchestrating email reports, decision verification, and state transitions."""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bug_agent.ai_analyzer import AnalysisResult
from bug_agent.config import AppConfig
from bug_agent.gmail_client import GmailClientBase, InboundEmail, MockGmailClient
from bug_agent.models import ApprovalRequest, ApprovalStatus, Task, TaskStatus
from bug_agent.task_store import TaskStore

logger = logging.getLogger(__name__)

# Strict decision regex:
# Investigation approval: "APPROVE TASK-..." or "REJECT TASK-..."
# Publication approval:   "APPROVE PUBLISH TASK-..." or "REJECT PUBLISH TASK-..."
PUB_COMMAND_PATTERN = re.compile(
    r"\b(APPROVE|REJECT)\s+PUBLISH\s+(TASK-[a-zA-Z0-9_\-]+)\b",
    re.IGNORECASE,
)
COMMAND_PATTERN = re.compile(
    r"\b(APPROVE|REJECT)\s+(TASK-[a-zA-Z0-9_\-]+)\b",
    re.IGNORECASE,
)


def _parse_iso(iso_str: str) -> datetime:
    """Parse ISO datetime string, supporting Python 3.10 and trailing 'Z'."""
    s = iso_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ApprovalManager:
    """Manages the lifecycle of bug fix approvals via Gmail."""

    def __init__(
        self,
        task_store: TaskStore,
        gmail_client: GmailClientBase,
        config: Optional[AppConfig] = None,
    ):
        self.task_store = task_store
        self.gmail_client = gmail_client
        self.config = config or AppConfig.load()

    def format_approval_email(self, task: Task, analysis: AnalysisResult, expires_at: str) -> Tuple[str, str]:
        """Construct concise, structured plain text and HTML emails."""
        files_str = "\n".join(f"  - {f.path}" for f in analysis.relevant_files) or "  (None identified)"
        repro_str = "\n".join(f"  {step}" for step in analysis.reproduction_steps) or "  (None provided)"

        plain_text = f"""[BUG DEFECT ANALYSIS REPORT]
Task ID: {task.task_id}
Repository: {task.repository}
Issue: #{task.issue_number} ({task.issue_url})
Status: AWAITING APPROVAL

--------------------------------------------------
DISCLAIMER: This analysis is AI-generated and UNVERIFIED.
No patches or tests have been executed on the target repository.
--------------------------------------------------

ISSUE SUMMARY:
{analysis.issue_summary}

CLASSIFICATION:
{analysis.bug_classification} (Confidence: {analysis.confidence_level})

SUPPORTING EVIDENCE:
{analysis.evidence_summary}

SUSPECTED ROOT CAUSE:
{analysis.suspected_root_cause}

RELEVANT FILES:
{files_str}

PROPOSED NEXT STEPS:
{analysis.suggested_fix}

REPRODUCTION / VERIFICATION STEPS:
{repro_str}

UNRESOLVED QUESTIONS / LIMITATIONS:
{', '.join(analysis.unresolved_questions) or 'None documented'}

--------------------------------------------------
DECISION INSTRUCTIONS:
To approve investigation & patch generation, reply with:
  APPROVE {task.task_id}

To reject, reply with:
  REJECT {task.task_id}

This approval request will expire on: {expires_at}
--------------------------------------------------
"""

        html_body = f"""<html>
<body style="font-family: Arial, sans-serif; line-height: 1.5; color: #333;">
  <div style="background-color: #f4f4f4; padding: 15px; border-left: 4px solid #0066cc;">
    <h2 style="margin-top: 0;">AI Defect Analysis: {task.repository} #{task.issue_number}</h2>
    <p><strong>Task ID:</strong> <code>{task.task_id}</code> | <strong>Status:</strong> AWAITING APPROVAL</p>
    <p><a href="{task.issue_url}">View original GitHub issue</a></p>
  </div>

  <div style="background-color: #fff3cd; padding: 10px; margin: 15px 0; border: 1px solid #ffeeba; color: #856404;">
    <strong>Notice:</strong> This analysis is AI-generated and unverified. Code changes have not been generated or tested.
  </div>

  <h3>Issue Summary</h3>
  <p>{analysis.issue_summary}</p>

  <h3>Assessment</h3>
  <p><strong>Classification:</strong> {analysis.bug_classification} (Confidence: {analysis.confidence_level})</p>
  <p><strong>Suspected Root Cause:</strong> {analysis.suspected_root_cause}</p>

  <h3>Supporting Evidence</h3>
  <p>{analysis.evidence_summary}</p>

  <h3>Suggested Next Step</h3>
  <p>{analysis.suggested_fix}</p>

  <div style="background-color: #e2e3e5; padding: 15px; margin-top: 20px; border-radius: 4px;">
    <h3 style="margin-top: 0;">Decision Required</h3>
    <p>Reply to this email with either:</p>
    <ul>
      <li><code>APPROVE {task.task_id}</code> &mdash; Authorize creating an isolated patch branch and running tests.</li>
      <li><code>REJECT {task.task_id}</code> &mdash; Dismiss this issue candidate.</li>
    </ul>
    <small>Expires on: {expires_at}</small>
  </div>
</body>
</html>"""

        return plain_text, html_body

    def request_approval_for_task(self, task: Task) -> Optional[ApprovalRequest]:
        """Send approval email for an ANALYZED task and transition it to AWAITING_APPROVAL."""
        if task.status != TaskStatus.ANALYZED:
            logger.info(f"Task {task.task_id} is in status {task.status.value}, skipping approval request.")
            return None

        if not self.config.approver_email:
            logger.error("Cannot request approval: APPROVER_EMAIL is not configured.")
            return None

        if not task.analysis_result:
            logger.error(f"Task {task.task_id} has no analysis_result.")
            return None

        analysis_dict = json.loads(task.analysis_result)
        analysis = AnalysisResult.from_dict(analysis_dict)

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(hours=self.config.approval_timeout_hours)).isoformat()

        subject = f"[APPROVAL REQUIRED] {task.task_id}: Bug Candidate in {task.repository} #{task.issue_number}"
        plain_text, html_body = self.format_approval_email(task, analysis, expires_at)

        try:
            logger.info(f"Sending approval request email to {self.config.approver_email} for task {task.task_id}...")
            msg_id, thread_id = self.gmail_client.send_email(
                to=self.config.approver_email,
                subject=subject,
                body_text=plain_text,
                body_html=html_body,
            )
        except Exception as e:
            logger.error(f"Failed sending approval email for {task.task_id}: {e}")
            return None

        # Transition task to AWAITING_APPROVAL
        self.task_store.update_task_status(
            task_id=task.task_id,
            new_status=TaskStatus.AWAITING_APPROVAL,
            extra_fields={"approval_requested_at": now.isoformat()},
        )

        # Record approval request in database
        req = self.task_store.record_approval_request(
            task_id=task.task_id,
            repository=task.repository,
            issue_number=task.issue_number,
            approver_email=self.config.approver_email.lower(),
            expires_at=expires_at,
            message_id=msg_id,
            thread_id=thread_id,
        )
        logger.info(f"Task {task.task_id} transitioned to AWAITING_APPROVAL (Request recorded).")
        return req

    def request_publication_approval_for_task(self, task: Task) -> bool:
        """Send publication review email for a TESTED task and transition it to AWAITING_PUBLICATION_APPROVAL."""
        from bug_agent.test_runner import DiffVerifier

        if task.status != TaskStatus.TESTED:
            logger.info(f"Task {task.task_id} is in status {task.status.value}, skipping publication approval (expected TESTED).")
            return False

        if not self.config.approver_email:
            logger.error("Cannot request publication approval: APPROVER_EMAIL is not configured.")
            return False

        if not task.patch_path or not Path(task.patch_path).is_file():
            logger.error(f"Cannot request publication approval: patch file missing for {task.task_id}.")
            return False

        patch_text = Path(task.patch_path).read_text(encoding="utf-8")
        patch_hash = DiffVerifier.compute_patch_hash(patch_text)

        test_info = json.loads(task.test_result) if task.test_result else {}
        analysis_info = json.loads(task.analysis_result) if task.analysis_result else {}

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(hours=self.config.approval_timeout_hours)).isoformat()

        plain_text = f"""[FINAL PUBLICATION REVIEW REQUIRED]
Task ID: {task.task_id}
Repository: {task.repository}
Issue: #{task.issue_number} ({task.issue_url})
Base Commit SHA: {task.base_commit_sha}
Patch SHA-256: {patch_hash}
Status: AWAITING PUBLICATION APPROVAL

--------------------------------------------------
DISCLAIMER: Tests executed and passed in sandbox.
Passing tests do not prove absolute correctness or guarantee the absence of regressions.
Review the diff and test summary carefully before approving.
--------------------------------------------------

INTENDED PULL REQUEST:
Title: Fix bug #{task.issue_number}: {task.issue_title}
Description:
  Automated bug fix proposed by AI Bug Agent for issue #{task.issue_number}.
  Analysis: {analysis_info.get('issue_summary', '')}
  Test Results: {test_info.get('test_summary', '')} (Exit code: {test_info.get('exit_code', 0)})

TEST EXECUTION SUMMARY:
  Command: {test_info.get('test_command', 'pytest')}
  Exit Code: {test_info.get('exit_code', 0)}
  Duration: {test_info.get('duration_seconds', 0.0):.2f}s
  Summary: {test_info.get('test_summary', '')}

UNIFIED DIFF PREVIEW:
{patch_text[:3000]}

--------------------------------------------------
DECISION INSTRUCTIONS:
To approve creating and publishing the GitHub draft pull request, reply with:
  APPROVE PUBLISH {task.task_id}

To reject publication, reply with:
  REJECT PUBLISH {task.task_id}

This publication request will expire on: {expires_at}
--------------------------------------------------
"""

        subject = f"[PUBLICATION APPROVAL REQUIRED] {task.task_id}: Publish PR for {task.repository} #{task.issue_number}"
        try:
            self.gmail_client.send_email(
                to=self.config.approver_email,
                subject=subject,
                body_text=plain_text,
            )
        except Exception as e:
            logger.error(f"Failed sending publication approval email for {task.task_id}: {e}")
            return False

        # Transition task to AWAITING_PUBLICATION_APPROVAL
        self.task_store.update_task_status(
            task_id=task.task_id,
            new_status=TaskStatus.AWAITING_PUBLICATION_APPROVAL,
        )

        # Record publication request in SQLite
        self.task_store.record_publication_request(
            task_id=task.task_id,
            repository=task.repository,
            base_commit_sha=task.base_commit_sha or "unknown",
            patch_hash=patch_hash,
            approver_email=self.config.approver_email.lower(),
            expires_at=expires_at,
        )
        logger.info(f"Task {task.task_id} transitioned to AWAITING_PUBLICATION_APPROVAL.")
        return True

    def process_incoming_decision(self, email_msg: InboundEmail) -> Tuple[bool, str]:
        """Process an inbound email for an approval or rejection command.

        Security checks:
        1. Authenticated sender must match configured approver_email.
        2. Replay prevention: message_id must not have been processed.
        3. Strict command parsing: only explicit APPROVE/REJECT TASK-... accepted.
        4. Task must exist and be in AWAITING_APPROVAL status.
        5. Request must not be expired.
        """
        # 1. Idempotency & replay check
        if self.task_store.is_message_processed(email_msg.message_id):
            return False, f"Message {email_msg.message_id} already processed (replay ignored)."

        # 2. Authenticated sender validation
        configured_approver = (self.config.approver_email or "").strip().lower()
        if not configured_approver or email_msg.sender.strip().lower() != configured_approver:
            return False, f"Unauthorized sender: '{email_msg.sender}' (expected '{configured_approver}')."

        # 3. Parse explicit command and check for contradictory commands
        combined_text = f"{email_msg.body or ''} {email_msg.subject or ''}"
        all_pub_matches = PUB_COMMAND_PATTERN.findall(combined_text)
        if all_pub_matches:
            actions_found = {m[0].upper() for m in all_pub_matches}
            if len(actions_found) > 1:
                return False, "Conflicting publication commands detected in email (both APPROVE and REJECT)."
            action, task_id = all_pub_matches[0]
            action = action.upper()
            task_id = task_id.strip()

            self.task_store.record_processed_message(
                message_id=email_msg.message_id,
                sender=email_msg.sender,
                command=f"{action} PUBLISH {task_id}",
                task_id=task_id,
            )

            task = self.task_store.get_task(task_id)
            if not task:
                return False, f"Task '{task_id}' not found in database."

            if task.status != TaskStatus.AWAITING_PUBLICATION_APPROVAL:
                return False, f"Task '{task_id}' is in status '{task.status.value}', not AWAITING_PUBLICATION_APPROVAL."

            pub_rec = self.task_store.get_publication_record(task_id)
            if pub_rec and pub_rec.get("expires_at"):
                now = datetime.now(timezone.utc)
                if now > _parse_iso(pub_rec["expires_at"]):
                    self.task_store.update_task_status(task_id, TaskStatus.EXPIRED)
                    return False, f"Publication request for task '{task_id}' expired at {pub_rec['expires_at']}."

            is_approved = (action == "APPROVE")
            self.task_store.record_publication_decision(task_id, approved=is_approved, approver=email_msg.sender)
            if not is_approved:
                self.task_store.update_task_status(task_id, TaskStatus.REJECTED)

            decision_str = "PUBLICATION_APPROVED" if is_approved else "REJECTED"
            logger.info(f"Publication decision for {task_id}: {decision_str}")
            return True, f"Publication {decision_str} for task {task_id} by {email_msg.sender}."

        all_std_matches = COMMAND_PATTERN.findall(combined_text)
        if not all_std_matches:
            return False, "No valid approval command found in email."

        actions_found = {m[0].upper() for m in all_std_matches}
        if len(actions_found) > 1:
            return False, "Conflicting approval commands detected in email (both APPROVE and REJECT)."

        action, task_id = all_std_matches[0]
        action = action.upper()
        task_id = task_id.strip()

        # Mark message as processed to prevent replay
        self.task_store.record_processed_message(
            message_id=email_msg.message_id,
            sender=email_msg.sender,
            command=f"{action} {task_id}",
            task_id=task_id,
        )

        # 4. Task validation
        task = self.task_store.get_task(task_id)
        if not task:
            return False, f"Task '{task_id}' not found in database."

        if task.status != TaskStatus.AWAITING_APPROVAL:
            return False, f"Task '{task_id}' is in status '{task.status.value}', not AWAITING_APPROVAL."

        # 5. Check expiration
        approval_req = self.task_store.get_approval_request(task_id)
        if approval_req and approval_req.expires_at:
            now = datetime.now(timezone.utc)
            expires_at_dt = _parse_iso(approval_req.expires_at)
            if now > expires_at_dt:
                self.task_store.update_task_status(task_id, TaskStatus.EXPIRED)
                return False, f"Approval request for task '{task_id}' expired at {approval_req.expires_at}."

        # 6. Apply atomic decision
        is_approved = (action == "APPROVE")
        self.task_store.record_approval_decision(
            task_id=task_id,
            approved=is_approved,
            approver=email_msg.sender,
        )

        status_str = "APPROVED" if is_approved else "REJECTED"
        logger.info(f"Successfully processed {action} for task {task_id}. Task is now {status_str}.")
        return True, f"Task {task_id} successfully {status_str} by {email_msg.sender}."

    def poll_and_process_replies(self) -> List[Tuple[bool, str]]:
        """Fetch unread emails and process any approval commands."""
        logger.info("Checking for inbound approval replies...")
        inbox = self.gmail_client.fetch_inbound_replies()
        logger.info(f"Found {len(inbox)} inbound email messages.")

        results = []
        for msg in inbox:
            outcome, detail = self.process_incoming_decision(msg)
            results.append((outcome, detail))
            logger.info(f"Processed email ({msg.message_id}): {detail}")

        return results

    def sweep_expired_tasks(self) -> List[str]:
        """Scan active approval requests and transition expired tasks to EXPIRED."""
        expired_task_ids: List[str] = []
        now = datetime.now(timezone.utc)

        # 1. Check investigation approvals
        awaiting_investigation = self.task_store.list_tasks(status=TaskStatus.AWAITING_APPROVAL)
        for t in awaiting_investigation:
            req = self.task_store.get_approval_request(t.task_id)
            if req and req.expires_at and now > _parse_iso(req.expires_at):
                self.task_store.update_task_status(t.task_id, TaskStatus.EXPIRED)
                expired_task_ids.append(t.task_id)
                logger.info(f"Task {t.task_id} transitioned to EXPIRED (approval timeout).")

        # 2. Check publication approvals
        awaiting_pub = self.task_store.list_tasks(status=TaskStatus.AWAITING_PUBLICATION_APPROVAL)
        for t in awaiting_pub:
            rec = self.task_store.get_publication_record(t.task_id)
            if rec and rec.get("expires_at") and now > _parse_iso(rec["expires_at"]):
                self.task_store.update_task_status(t.task_id, TaskStatus.EXPIRED)
                expired_task_ids.append(t.task_id)
                logger.info(f"Task {t.task_id} transitioned to EXPIRED (publication timeout).")

        return expired_task_ids

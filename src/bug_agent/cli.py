"""Command-line interface for the AI Bug Agent scanner."""

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import List

from bug_agent.ai_analyzer import MockAIProvider
from bug_agent.approval_manager import ApprovalManager
from bug_agent.candidate_filter import CandidateFilter
from bug_agent.config import (
    ALLOWED_REPOSITORY,
    AppConfig,
    ConfigurationError,
    is_allowed_repository,
    validate_repository_url_or_name,
)
from bug_agent.github_client import GitHubClient
from bug_agent.gmail_client import GmailClient, MockGmailClient
from bug_agent.issue_scanner import IssueScanner
from bug_agent.models import RepositoryScanResult, TaskStatus
from bug_agent.patch_generator import PatchGenerator
from bug_agent.publisher import GitHubPublisher
from bug_agent.state_snapshot import StateSnapshotError, load_state_snapshot, save_state_snapshot
from bug_agent.task_store import TaskStore
from bug_agent.test_runner import DockerTestRunner, MockTestRunner, SandboxedVerifier
from bug_agent.workflow import DefectWorkflow


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _handle_send_issue(
    target_str: str,
    task_store: TaskStore,
    config: AppConfig,
    mock_gmail: bool,
    logger: logging.Logger,
) -> bool:
    has_token = bool(config.gmail_token_json or os.environ.get("GMAIL_TOKEN_JSON") or os.path.isfile("token.json"))
    gmail_ready = mock_gmail or bool(config.approver_email and has_token)
    if not gmail_ready:
        if not config.approver_email:
            logger.error("Recipient email is missing. Supply --email your@gmail.com or set APPROVER_EMAIL.")
        if not mock_gmail and not has_token:
            logger.error("Live Gmail approval requires GMAIL_TOKEN_JSON or token.json generated via generate_token.py.")
        return False

    gmail_client = MockGmailClient(approver_email=config.approver_email or "approver@example.com") if mock_gmail else GmailClient()
    approval_mgr = ApprovalManager(task_store=task_store, gmail_client=gmail_client, config=config)

    print("\n" + "=" * 60)
    print(f" SENDING ISSUE TO GMAIL: {target_str}")
    print("=" * 60)

    task_to_send = None
    if target_str.isdigit():
        issue_num = int(target_str)
        task_id_guess = f"TASK-pytorch-ignite-{issue_num}"
        task_to_send = task_store.get_task(task_id_guess)
        if not task_to_send:
            for t in task_store.list_tasks():
                if t.issue_number == issue_num and is_allowed_repository(t.repository):
                    task_to_send = t
                    break
        if not task_to_send:
            try:
                gh_client = GitHubClient(token=config.github_token, timeout=config.request_timeout_seconds)
                raw_issue = gh_client.get_issue("pytorch/ignite", issue_num)
                base_sha = gh_client.get_branch_sha("pytorch/ignite", "master") or gh_client.get_branch_sha("pytorch/ignite", "main")
                task_to_send, _ = task_store.record_discovered_task(
                    repository="pytorch/ignite",
                    issue_number=issue_num,
                    issue_title=raw_issue.get("title", f"Issue #{issue_num}"),
                    issue_url=raw_issue.get("html_url", f"https://github.com/pytorch/ignite/issues/{issue_num}"),
                    issue_body=raw_issue.get("body", ""),
                    labels=[lbl.get("name") if isinstance(lbl, dict) else str(lbl) for lbl in raw_issue.get("labels", [])],
                    author=raw_issue.get("user", {}).get("login", "") if isinstance(raw_issue.get("user"), dict) else "",
                    base_commit_sha=base_sha,
                )
                print(f"  Fetched issue #{issue_num} from GitHub and recorded as {task_to_send.task_id}.")
            except Exception as exc:
                print(f"[FAILED] Could not fetch issue #{issue_num} from GitHub: {exc}")
                return False
    else:
        task_to_send = task_store.get_task(target_str)

    if not task_to_send:
        print(f"[FAILED] Task or issue '{target_str}' not found in database.")
        return False

    if not is_allowed_repository(task_to_send.repository):
        print(f"[FAILED] Task repository '{task_to_send.repository}' is out of scope. Scope is restricted to https://github.com/{ALLOWED_REPOSITORY}.")
        return False

    if not task_to_send.analysis_result and task_to_send.status == TaskStatus.DISCOVERED:
        print(f"  Running offline analysis on {task_to_send.task_id} before sending...")
        workflow = DefectWorkflow(task_store=task_store, ai_provider=MockAIProvider(), config=config)
        workflow.analyze_task(task_to_send)
        task_to_send = task_store.get_task(task_to_send.task_id)

    ok, msg = approval_mgr.send_issue_notification(task_to_send, recipient_email=config.approver_email)
    if ok:
        print(f"[SUCCESS] {msg}")
    else:
        print(f"[FAILED] {msg}")
    print("=" * 60 + "\n")
    return ok


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Multi-Repository Bug Agent - Automated Defect Discovery, Offline Deterministic Analysis, Sandboxed Testing & Patch Publishing"
    )
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default="repos.json",
        help="Path to repository configuration JSON file (default: repos.json)",
    )
    parser.add_argument(
        "--repo",
        "-r",
        type=str,
        action="append",
        help="Specific repository to scan (can be specified multiple times)",
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        help="Override max issues to fetch per repository",
    )
    parser.add_argument(
        "--state",
        "-s",
        type=str,
        choices=["open", "closed", "all"],
        default="open",
        help="Issue state to scan for (default: open)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        help="Path to write structured JSON output",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        help="Path to SQLite database file (overrides config/DATABASE_PATH)",
    )
    parser.add_argument(
        "--state-json",
        type=str,
        default=os.environ.get("STATE_JSON_PATH"),
        help="Optional non-secret JSON state checkpoint path (SQLite remains authoritative)",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Disable recording scanned issues in SQLite task store",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Run offline deterministic defect analysis on discovered issues",
    )
    parser.add_argument(
        "--ai-provider",
        type=str,
        choices=["mock"],
        default="mock",
        help="Offline provider to use for defect analysis (default: mock)",
    )
    parser.add_argument(
        "--request-approvals",
        action="store_true",
        help="Send Gmail approval requests for analyzed issues awaiting decision",
    )
    parser.add_argument(
        "--send-issue",
        type=str,
        metavar="TASK_OR_ISSUE",
        help="Send a specific issue or task analysis report directly to Gmail",
    )
    parser.add_argument(
        "--email",
        "--approver-email",
        type=str,
        dest="email",
        help="Recipient Gmail address to send issue reports and approval requests to",
    )
    parser.add_argument(
        "--check-approvals",
        action="store_true",
        help="Check Gmail for incoming approval/rejection replies",
    )
    parser.add_argument(
        "--mock-gmail",
        action="store_true",
        help="Use mock Gmail client instead of live Google API OAuth",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Display all tasks and approval status in database",
    )
    parser.add_argument(
        "--generate-patch",
        type=str,
        metavar="TASK_ID",
        help="Generate an isolated code patch for a specified APPROVED task ID",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run patch generation without committing changes or updating database state",
    )
    parser.add_argument(
        "--run-tests",
        type=str,
        metavar="TASK_ID",
        help="Execute sandboxed tests for a specified PATCH_READY task ID",
    )
    parser.add_argument(
        "--mock-sandbox",
        action="store_true",
        help="Use deterministic mock test runner instead of requiring local Docker container sandbox",
    )
    parser.add_argument(
        "--request-publication",
        action="store_true",
        help="Send Gmail review and approval requests for TESTED issues awaiting publication decision",
    )
    parser.add_argument(
        "--publish",
        type=str,
        metavar="TASK_ID",
        help="Create dedicated branch and draft PR on GitHub for an authorized task",
    )

    args = parser.parse_args(argv)

    try:
        config = AppConfig.load(args.config)
    except ConfigurationError as e:
        print(f"Error loading configuration: {e}", file=sys.stderr)
        return 1

    configure_logging(config.log_level)
    logger = logging.getLogger("bug_agent.cli")

    if args.email:
        config.approver_email = args.email.strip().lower()

    if args.limit:
        config.issues_per_repository = args.limit

    if args.db_path:
        config.database_path = args.db_path

    task_store = None
    candidate_filter = None
    if not args.no_db:
        try:
            task_store = TaskStore(db_path=config.database_path)
            candidate_filter = CandidateFilter(task_store=task_store)
        except Exception as e:
            logger.warning(f"Failed to initialize SQLite task store at {config.database_path}: {e}")

    if args.state_json:
        try:
            previous_state = load_state_snapshot(args.state_json)
        except StateSnapshotError as exc:
            logger.error("Cannot resume from state checkpoint: %s", exc)
            return 1
        if previous_state:
            logger.info("Loaded prior state checkpoint for %d tasks; SQLite remains authoritative.", len(previous_state["tasks"]))

    # If --status is requested as a standalone query, inspect the database directly without scanning GitHub
    if args.status and not (
        args.repo or args.limit or args.analyze or args.request_approvals or
        args.check_approvals or args.generate_patch or args.run_tests or
        args.request_publication or args.publish or args.send_issue
    ):
        if task_store is None:
            print("Error: SQLite task store is disabled (--no-db) or failed to initialize.", file=sys.stderr)
            return 1
        print("\n" + "=" * 70)
        print(" DATABASE TASK & APPROVAL STATUS")
        print("=" * 70)
        all_tasks = task_store.list_tasks()
        for t in all_tasks:
            req = task_store.get_approval_request(t.task_id)
            approval_info = f" | Approval: {req.status.value}" if req else ""
            print(f"  - [{t.status.value}] {t.task_id}: {t.issue_title}{approval_info}")
        print("=" * 70 + "\n")
        return 0

    # If --send-issue is requested as a standalone command without scanning GitHub
    if args.send_issue and not (
        args.repo or args.limit or args.analyze or args.request_approvals or
        args.check_approvals or args.generate_patch or args.run_tests or
        args.request_publication or args.publish
    ):
        if task_store is None:
            print("Error: SQLite task store is disabled (--no-db) or failed to initialize.", file=sys.stderr)
            return 1
        ok = _handle_send_issue(
            target_str=args.send_issue.strip(),
            task_store=task_store,
            config=config,
            mock_gmail=args.mock_gmail,
            logger=logger,
        )
        return 0 if ok else 1

    repos_to_scan: List[str] = []
    if args.repo:
        for r in args.repo:
            try:
                valid_repo = validate_repository_url_or_name(r)
                if valid_repo not in repos_to_scan:
                    repos_to_scan.append(valid_repo)
            except ConfigurationError as err:
                print(f"Error: {err}", file=sys.stderr)
                return 1
    else:
        repos_to_scan = config.repositories

    scanner = IssueScanner(
        config=config,
        task_store=task_store,
        candidate_filter=candidate_filter,
    )
    results = scanner.scan_all(repositories=repos_to_scan, state=args.state)

    total_issues = sum(len(res.issues) for res in results)
    successful_repos = sum(1 for res in results if res.success)
    failed_repos = len(results) - successful_repos

    print("\n" + "=" * 60)
    print(" SCAN SUMMARY")
    print("=" * 60)
    for res in results:
        status = "OK" if res.success else "FAILED"
        print(f"[{status}] {res.repository}: {len(res.issues)} issues")
        if not res.success:
            print(f"      Error: {res.error_message}")
        elif res.raw_count > 0:
            print(f"      Filtering stages:")
            print(f"        - Raw items fetched from API: {res.raw_count}")
            if res.pr_excluded_count > 0:
                print(f"        - Excluded as pull requests: {res.pr_excluded_count}")
            if res.filter_exclusions:
                for reason, count in sorted(res.filter_exclusions.items()):
                    print(f"        - Excluded by candidate filter ({reason}): {count}")
            print(f"        - Final candidate issues: {len(res.issues)}")
    print("-" * 60)
    print(f"Total Repositories: {len(results)} (Success: {successful_repos}, Failed: {failed_repos})")
    print(f"Total Candidate Issues Discovered: {total_issues}")
    print("=" * 60 + "\n")

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        serializable = [r.to_dict() for r in results]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)
        print(f"Detailed structured results written to: {out_path.resolve()}")

    # Milestone 3: Offline Defect Analysis
    analysis_stage_failed = False
    if args.analyze and task_store is not None:
        print("\n" + "=" * 60)
        print(f" RUNNING DEFECT ANALYSIS (Provider: {args.ai_provider} - Offline Deterministic)")
        print("=" * 60)

        ai_provider = MockAIProvider()
        workflow = DefectWorkflow(task_store=task_store, ai_provider=ai_provider, config=config)
        pending_analysis_count = len(task_store.list_tasks(status=TaskStatus.DISCOVERED))
        analysis_results = workflow.analyze_all_discovered()
        analysis_stage_failed = len(analysis_results) != pending_analysis_count

        print(f"Total Discovered Issues Analyzed: {len(analysis_results)}")
        for ar in analysis_results:
            print(f"  [{ar.bug_classification}] {ar.task_id} (Confidence: {ar.confidence_level})")
            print(f"    Summary: {ar.issue_summary}")
            print(f"    Suspected Cause: {ar.suspected_root_cause}")
        print("=" * 60 + "\n")

    # Milestone 4: Gmail Approval Management
    approval_stage_failed = False
    if task_store is not None:
        has_token = bool(config.gmail_token_json or os.environ.get("GMAIL_TOKEN_JSON") or os.path.isfile("token.json"))
        gmail_ready = args.mock_gmail or bool(config.approver_email and has_token)
        if (args.request_approvals or args.check_approvals or args.send_issue) and not gmail_ready:
            if not config.approver_email:
                logger.error("Recipient email is missing. Supply --email your@gmail.com or set APPROVER_EMAIL.")
            if not args.mock_gmail and not has_token:
                logger.error("Live Gmail approval requires GMAIL_TOKEN_JSON or token.json generated via generate_token.py.")
            approval_stage_failed = True
        gmail_client = MockGmailClient(approver_email=config.approver_email or "approver@example.com") if args.mock_gmail else GmailClient()
        approval_mgr = ApprovalManager(task_store=task_store, gmail_client=gmail_client, config=config)

        if args.send_issue and gmail_ready:
            ok = _handle_send_issue(
                target_str=args.send_issue.strip(),
                task_store=task_store,
                config=config,
                mock_gmail=args.mock_gmail,
                logger=logger,
            )
            if not ok:
                approval_stage_failed = True

        if args.request_approvals and gmail_ready:
            print("\n" + "=" * 60)
            print(" SENDING GMAIL APPROVAL REQUESTS")
            print("=" * 60)
            analyzed_tasks = task_store.list_tasks(status=TaskStatus.ANALYZED)
            print(f"Tasks eligible for approval request: {len(analyzed_tasks)}")
            for t in analyzed_tasks:
                req = approval_mgr.request_approval_for_task(t)
                if req:
                    print(f"  [SENT] Approval request sent for {t.task_id} (Expires: {req.expires_at})")
                else:
                    print(f"  [FAILED] Could not send request for {t.task_id}")
                    approval_stage_failed = True
            print("=" * 60 + "\n")

        if args.check_approvals and gmail_ready:
            print("\n" + "=" * 60)
            print(" CHECKING GMAIL FOR APPROVAL DECISIONS")
            print("=" * 60)
            results = approval_mgr.poll_and_process_replies()
            print(f"Processed {len(results)} replies:")
            for ok, detail in results:
                prefix = "[OK]" if ok else "[REJECTED/IGNORED]"
                print(f"  {prefix} {detail}")
            print("=" * 60 + "\n")

        if args.status:
            print("\n" + "=" * 70)
            print(" DATABASE TASK & APPROVAL STATUS")
            print("=" * 70)
            all_tasks = task_store.list_tasks()
            for t in all_tasks:
                req = task_store.get_approval_request(t.task_id)
                approval_info = f" | Approval: {req.status.value}" if req else ""
                print(f"  - [{t.status.value}] {t.task_id}: {t.issue_title}{approval_info}")
            print("=" * 70 + "\n")

        # Milestone 5: Isolated Patch Generation
        if args.generate_patch:
            target_task_id = args.generate_patch.strip()
            task_obj = task_store.get_task(target_task_id)
            if task_obj and not is_allowed_repository(task_obj.repository):
                print(f"[FAILED] Task repository '{task_obj.repository}' is out of scope. Modifying or creating a patch is restricted to https://github.com/{ALLOWED_REPOSITORY}.")
                return 1

            print("\n" + "=" * 60)
            print(f" PATCH GENERATION FOR {target_task_id} (Dry Run: {args.dry_run})")
            print("=" * 60)

            ai_provider = MockAIProvider()
            patch_gen = PatchGenerator(task_store=task_store, ai_provider=ai_provider, config=config)

            success, patch_rec, message = patch_gen.generate_patch_for_task(target_task_id, dry_run=args.dry_run)
            if success and patch_rec:
                print(f"[SUCCESS] {message}")
                print(f"  Base SHA: {patch_rec.base_commit_sha}")
                print(f"  Modified Files: {', '.join(patch_rec.modified_files)}")
                print(f"  Patch File: {patch_rec.patch_path}")
                print("\n--- UNIFIED DIFF PREVIEW ---")
                print(patch_rec.diff_content)
                print("----------------------------\n")
            else:
                print(f"[FAILED] {message}")
            print("=" * 60 + "\n")

        # Milestone 6: Sandboxed Test Execution
        if args.run_tests:
            target_task_id = args.run_tests.strip()
            print("\n" + "=" * 60)
            print(f" SANDBOXED TEST EXECUTION FOR {target_task_id}")
            print("=" * 60)

            runner = MockTestRunner() if args.mock_sandbox else DockerTestRunner()
            verifier = SandboxedVerifier(task_store=task_store, test_runner=runner, config=config)

            passed, test_rec, msg = verifier.verify_task_patch(target_task_id)
            if test_rec:
                print(f"Outcome: {test_rec.classification}")
                print(f"  Duration: {test_rec.duration_seconds:.2f}s | Exit Code: {test_rec.exit_code}")
                print(f"  Sandbox Profile: {test_rec.sandbox_profile}")
                print(f"  Test Command: {test_rec.test_command}")
                print(f"  Summary: {test_rec.test_summary}")
                print(f"  Explanation: {test_rec.explanation}")
            else:
                print(f"[ERROR] {msg}")
            print("=" * 60 + "\n")

        # Milestone 7: Publication Review & Draft PR Creation
        if args.request_publication:
            print("\n" + "=" * 60)
            print(" SENDING PUBLICATION REVIEW & APPROVAL REQUESTS")
            print("=" * 60)
            tested_tasks = task_store.list_tasks(status=TaskStatus.TESTED)
            print(f"Tasks eligible for publication review: {len(tested_tasks)}")
            for t in tested_tasks:
                ok = approval_mgr.request_publication_approval_for_task(t)
                status_label = "[SENT]" if ok else "[FAILED]"
                print(f"  {status_label} Publication review email sent for {t.task_id}")
            print("=" * 60 + "\n")

        if args.publish:
            target_task_id = args.publish.strip()
            task_obj = task_store.get_task(target_task_id)
            if task_obj and not is_allowed_repository(task_obj.repository):
                print(f"[FAILED] Task repository '{task_obj.repository}' is out of scope. Publication is restricted to https://github.com/{ALLOWED_REPOSITORY}.")
                return 1

            print("\n" + "=" * 60)
            print(f" PUBLISHING DRAFT PULL REQUEST FOR {target_task_id} (Dry Run: {args.dry_run})")
            print("=" * 60)

            publisher = GitHubPublisher(task_store=task_store, config=config)
            success, pub_result, msg = publisher.publish_pull_request(target_task_id, dry_run=args.dry_run)
            if success and pub_result:
                print(f"[SUCCESS] {msg}")
                print(f"  Branch: {pub_result.branch_name}")
                print(f"  PR #{pub_result.pr_number}: {pub_result.pr_url}")
                print(f"  Commit: {pub_result.commit_sha}")
            else:
                print(f"[FAILED] {msg}")
            print("=" * 60 + "\n")

    if task_store is not None and args.state_json:
        try:
            save_state_snapshot(task_store, args.state_json)
        except OSError as exc:
            logger.error("Could not save state checkpoint: %s", type(exc).__name__)
            return 1

    if approval_stage_failed or analysis_stage_failed:
        return 1
    return 0 if failed_repos == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

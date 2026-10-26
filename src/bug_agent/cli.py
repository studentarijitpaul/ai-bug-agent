"""Command-line interface for the AI Bug Agent scanner."""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List

from bug_agent.ai_analyzer import MockAIProvider
from bug_agent.approval_manager import ApprovalManager
from bug_agent.candidate_filter import CandidateFilter
from bug_agent.config import AppConfig, ConfigurationError
from bug_agent.codex_adapter import CodexAdapter
from bug_agent.gmail_client import GmailClient, MockGmailClient
from bug_agent.issue_scanner import IssueScanner
from bug_agent.models import RepositoryScanResult, TaskStatus
from bug_agent.patch_generator import PatchGenerator
from bug_agent.publisher import GitHubPublisher
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


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Multi-Repository AI Bug Agent - Automated Defect Discovery, Analysis, Sandboxed Testing & Patch Publishing"
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
        "--no-db",
        action="store_true",
        help="Disable recording scanned issues in SQLite task store",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Run AI defect analysis on discovered issues",
    )
    parser.add_argument(
        "--ai-provider",
        type=str,
        choices=["mock", "codex"],
        default="mock",
        help="AI provider to use for defect analysis (default: mock)",
    )
    parser.add_argument(
        "--request-approvals",
        action="store_true",
        help="Send Gmail approval requests for analyzed issues awaiting decision",
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
        "--approver",
        type=str,
        help="Override approver email address",
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

    if args.limit:
        config.issues_per_repository = args.limit

    if args.db_path:
        config.database_path = args.db_path

    if args.approver:
        config.approver_email = args.approver

    task_store = None
    candidate_filter = None
    if not args.no_db:
        try:
            task_store = TaskStore(db_path=config.database_path)
            candidate_filter = CandidateFilter(task_store=task_store)
        except Exception as e:
            logger.warning(f"Failed to initialize SQLite task store at {config.database_path}: {e}")

    # If --status is requested as a standalone query, inspect the database directly without scanning GitHub
    if args.status and not (
        args.repo or args.limit or args.analyze or args.request_approvals or
        args.check_approvals or args.generate_patch or args.run_tests or
        args.request_publication or args.publish
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

    repos_to_scan = args.repo if args.repo else config.repositories

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

    # Milestone 3: Optional Defect Analysis
    if args.analyze and task_store is not None:
        print("\n" + "=" * 60)
        print(f" RUNNING AI DEFECT ANALYSIS (Provider: {args.ai_provider})")
        print("=" * 60)

        ai_provider = MockAIProvider() if args.ai_provider == "mock" else CodexAdapter()
        workflow = DefectWorkflow(task_store=task_store, ai_provider=ai_provider, config=config)
        analysis_results = workflow.analyze_all_discovered()

        print(f"Total Discovered Issues Analyzed: {len(analysis_results)}")
        for ar in analysis_results:
            print(f"  [{ar.bug_classification}] {ar.task_id} (Confidence: {ar.confidence_level})")
            print(f"    Summary: {ar.issue_summary}")
            print(f"    Suspected Cause: {ar.suspected_root_cause}")
        print("=" * 60 + "\n")

    # Milestone 4: Gmail Approval Management
    if task_store is not None:
        gmail_client = MockGmailClient(approver_email=config.approver_email or "approver@example.com") if args.mock_gmail else GmailClient(
            credentials_file=config.gmail_credentials_file,
            token_file=config.gmail_token_file,
            credentials_json=config.gmail_credentials_json,
            token_json=config.gmail_token_json,
        )
        approval_mgr = ApprovalManager(task_store=task_store, gmail_client=gmail_client, config=config)

        if args.request_approvals:
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
            print("=" * 60 + "\n")

        if args.check_approvals:
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
            print("\n" + "=" * 60)
            print(f" PATCH GENERATION FOR {target_task_id} (Dry Run: {args.dry_run})")
            print("=" * 60)

            ai_provider = MockAIProvider() if args.ai_provider == "mock" else CodexAdapter()
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

    return 0 if failed_repos == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

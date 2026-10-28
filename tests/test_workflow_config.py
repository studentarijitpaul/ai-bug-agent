"""Regression checks for the GitHub Actions approval workflow."""

import re
from pathlib import Path


def test_manual_live_approval_run_resends_pending_mock_requests():
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "agent.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    live_resend = re.search(
        r'(?m)^\s*if \[\[ "\$GMAIL_MODE" == "LIVE OAUTH" \]\]; then\s*\n'
        r'\s*RESEND_MOCK_ARGS\+=\(--resend-mock-approvals\)',
        workflow,
    )
    analyze_operation = re.search(
        r'(?ms)^[ ]{12}analyze_and_request\)(.*?)^[ ]{14};;',
        workflow,
    )

    assert live_resend is not None
    assert '("refresh_token" in d)' in workflow
    assert analyze_operation is not None
    assert '"${RESEND_MOCK_ARGS[@]}"' in analyze_operation.group(1)

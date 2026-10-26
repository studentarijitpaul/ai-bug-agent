"""GitHub Copilot Programmatic Adapter.

Investigation and Integration Status:
- GitHub provides the official `github-copilot-sdk` and GitHub Copilot CLI for agent integration.
- Standard individual / Student Developer Pack subscriptions DO NOT provide direct raw LLM API
  keys or REST endpoints for custom standalone applications without the Copilot CLI runtime.
- For unattended / headless workflows, Copilot requires an authenticated session (`copilot auth login`)
  and non-interactive execution mode.
- When Copilot CLI or the SDK is not installed or authenticated, this adapter gracefully signals
  unavailability and falls back to a clean documented error with setup guidance.
"""

import json
import logging
import os
import shutil
import subprocess
from typing import Dict, Optional

from bug_agent.ai_analyzer import (
    AIProviderBase,
    AIProviderError,
    AnalysisResult,
    ContextBuilder,
    ResponseValidator,
    SYSTEM_SAFETY_PROMPT,
)

logger = logging.getLogger(__name__)


class CopilotAdapter(AIProviderBase):
    """Adapter for interacting with GitHub Copilot programmatically."""

    def __init__(self, cli_path: Optional[str] = None, timeout_seconds: int = 45):
        self.cli_path = cli_path or os.environ.get("COPILOT_CLI_PATH") or shutil.which("copilot") or shutil.which("gh")
        self.timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "github-copilot"

    def is_available(self) -> bool:
        """Check whether the GitHub Copilot CLI or GH Copilot extension is installed and available."""
        if not self.cli_path:
            return False
        if not (os.path.exists(self.cli_path) or shutil.which(self.cli_path) is not None):
            return False
        try:
            res = subprocess.run([self.cli_path, "--version"], capture_output=True, text=True, timeout=5)
            if res.returncode != 0 or "not found" in (res.stdout + res.stderr).lower():
                return False
            return True
        except Exception:
            return False

    def get_setup_instructions(self) -> str:
        """Instructions for configuring GitHub Copilot for programmatic CLI execution."""
        return """
GitHub Copilot Programmatic Setup Guide:
=========================================
1. Subscription Check:
   - Verify that your GitHub account has an active GitHub Copilot entitlement (Student, Individual, or Business).
   - Note: GitHub Copilot does NOT provide direct OpenAI-style API keys for student accounts.

2. CLI Installation:
   - Install GitHub Copilot CLI:
     npm install -g @github/copilot-cli
     # OR via GitHub CLI extension:
     gh extension install github/gh-copilot

3. Authentication:
   - Authenticate locally on your system:
     copilot auth login
     # OR:
     gh auth login --web

4. Headless / Programmatic Mode:
   - The adapter invokes the Copilot CLI non-interactively using prompt flags.
   - For CI/CD or automated pipelines, export COPILOT_CLI_PATH or pre-authenticate your machine.
"""

    def analyze_defect(
        self,
        task_id: str,
        repository: str,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        file_contexts: Optional[Dict[str, str]] = None,
    ) -> AnalysisResult:
        if not self.is_available():
            raise AIProviderError(
                f"GitHub Copilot CLI is not installed or not found on PATH.\n{self.get_setup_instructions()}"
            )

        prompt = ContextBuilder.build_prompt(
            task_id=task_id,
            repository=repository,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            file_contexts=file_contexts,
        )

        full_prompt = f"{SYSTEM_SAFETY_PROMPT}\n\n{prompt}"

        # Attempt invocation through copilot CLI non-interactive mode
        cmd = [self.cli_path, "-p", full_prompt, "-s"]
        try:
            logger.info(f"Invoking Copilot CLI for task {task_id} (timeout={self.timeout_seconds}s)...")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise AIProviderError(f"Copilot CLI timed out after {self.timeout_seconds}s for task {task_id}")
        except OSError as e:
            raise AIProviderError(f"Failed to execute Copilot CLI at '{self.cli_path}': {e}")

        if result.returncode != 0:
            err = result.stderr.strip() or f"Process exited with code {result.returncode}"
            raise AIProviderError(f"Copilot CLI failed: {err}")

        # Validate and return structured result
        analysis = ResponseValidator.validate_and_parse(result.stdout, expected_task_id=task_id)
        analysis.provider_name = self.name
        return analysis

    def propose_patch(
        self,
        task_id: str,
        repository: str,
        analysis: AnalysisResult,
        file_contents: Dict[str, str],
    ) -> Dict[str, str]:
        if not self.is_available():
            raise AIProviderError(
                f"GitHub Copilot CLI is not installed or not found on PATH.\n{self.get_setup_instructions()}"
            )
        # Copilot CLI interactive edit mode requires local environment; raise explicit adapter guidance
        raise AIProviderError(
            "Programmatic file patch generation via Copilot CLI requires interactive approval or Copilot SDK setup.\n"
            "Use MockAIProvider for local testing or configure Copilot SDK tool calling."
        )

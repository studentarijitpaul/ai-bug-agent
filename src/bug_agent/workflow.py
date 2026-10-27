"""Workflow engine for analyzing discovered defect candidates using an AI provider."""

import json
import logging
import re
from typing import Dict, List, Optional

from bug_agent.ai_analyzer import AIProviderBase, AIProviderError, AnalysisResult, MockAIProvider
from bug_agent.config import ALLOWED_REPOSITORY, AppConfig, is_allowed_repository
from bug_agent.github_client import GitHubClient
from bug_agent.models import Task, TaskStatus
from bug_agent.task_store import TaskStore

logger = logging.getLogger(__name__)

# Heuristic regex to extract file paths mentioned in issue descriptions or logs
FILE_PATH_PATTERN = re.compile(
    r'(?:[a-zA-Z0-9_\-.]+/)+[a-zA-Z0-9_\-.]+\.(?:py|sh|yaml|yml|json|md)',
    re.IGNORECASE,
)


class DefectWorkflow:
    """Orchestrates defect analysis across discovered tasks in the task store."""

    def __init__(
        self,
        task_store: TaskStore,
        ai_provider: Optional[AIProviderBase] = None,
        github_client: Optional[GitHubClient] = None,
        config: Optional[AppConfig] = None,
    ):
        self.task_store = task_store
        self.config = config or AppConfig.load()
        self.ai_provider = ai_provider or MockAIProvider()
        self.github_client = github_client or GitHubClient(token=self.config.github_token)

    def extract_candidate_file_paths(self, issue_body: str) -> List[str]:
        """Extract candidate file paths mentioned in the issue body with defensive limits."""
        if not issue_body:
            return []
        matches = FILE_PATH_PATTERN.findall(issue_body)
        unique_paths = []
        for p in matches:
            clean = p.strip().strip("'\"`")
            # Defensive check: ensure no path traversal
            if ".." in clean or clean.startswith("/"):
                continue
            if clean not in unique_paths:
                unique_paths.append(clean)
            if len(unique_paths) >= 5:  # Cap at 5 files per issue
                break
        return unique_paths

    def retrieve_file_contexts(self, repository: str, file_paths: List[str]) -> Dict[str, str]:
        """Fetch file contents from GitHub with security and size limits."""
        contexts: Dict[str, str] = {}
        for path in file_paths:
            try:
                content = self.github_client.get_file_content(repository=repository, path=path)
                if content:
                    contexts[path] = content
            except Exception as e:
                logger.debug(f"Could not retrieve context for file {path}: {e}")
        return contexts

    def analyze_task(self, task: Task) -> Optional[AnalysisResult]:
        """Analyze a single task if in DISCOVERED state.

        Transitions:
        - DISCOVERED -> ANALYZED (on success)
        - DISCOVERED -> FAILED (on provider or processing error)
        """
        if task.status != TaskStatus.DISCOVERED:
            logger.info(f"Skipping task {task.task_id}; status is {task.status.value} (expected DISCOVERED).")
            return None

        # Scope restriction: Analyze only pytorch/ignite
        if not is_allowed_repository(task.repository):
            logger.warning(
                f"Skipping task {task.task_id}; repository '{task.repository}' is out of scope (only https://github.com/{ALLOWED_REPOSITORY} allowed)."
            )
            return None

        logger.info(f"Analyzing task {task.task_id} using {self.ai_provider.name}...")

        # 1. Inspect issue body for mentioned source files
        candidate_files = self.extract_candidate_file_paths(task.issue_body or "")
        file_contexts = self.retrieve_file_contexts(task.repository, candidate_files)

        # 2. Invoke AI Provider
        try:
            analysis = self.ai_provider.analyze_defect(
                task_id=task.task_id,
                repository=task.repository,
                issue_number=task.issue_number,
                issue_title=task.issue_title,
                issue_body=task.issue_body or "",
                file_contexts=file_contexts,
            )

            # 3. Save result and transition state
            analysis_json = json.dumps(analysis.to_dict(), indent=2)
            self.task_store.update_task_status(
                task_id=task.task_id,
                new_status=TaskStatus.ANALYZED,
                extra_fields={"analysis_result": analysis_json},
            )
            logger.info(f"Successfully analyzed task {task.task_id} (Classification: {analysis.bug_classification}).")
            return analysis

        except AIProviderError as e:
            err_msg = f"AI Provider error during analysis: {e}"
            logger.error(f"Failed analyzing task {task.task_id}: {err_msg}")
            self.task_store.update_task_status(
                task_id=task.task_id,
                new_status=TaskStatus.FAILED,
                error_message=err_msg,
            )
            return None
        except Exception as e:
            err_msg = f"Unexpected error during task analysis: {e}"
            logger.exception(f"Unexpected exception for task {task.task_id}")
            self.task_store.update_task_status(
                task_id=task.task_id,
                new_status=TaskStatus.FAILED,
                error_message=err_msg,
            )
            return None

    def analyze_all_discovered(self, repository: Optional[str] = None) -> List[AnalysisResult]:
        """Analyze all tasks currently in DISCOVERED status."""
        discovered_tasks = self.task_store.list_tasks(repository=repository, status=TaskStatus.DISCOVERED)
        logger.info(f"Found {len(discovered_tasks)} tasks awaiting analysis.")

        results: List[AnalysisResult] = []
        for task in discovered_tasks:
            res = self.analyze_task(task)
            if res:
                results.append(res)

        return results

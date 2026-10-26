"""Tests for AI provider abstraction, context building, response validation, and Copilot adapter."""

import json
import pytest
from unittest.mock import MagicMock, patch

from bug_agent.ai_analyzer import (
    AIProviderError,
    AIResponseFormatError,
    AnalysisResult,
    ContextBuilder,
    MockAIProvider,
    ResponseValidator,
)
from bug_agent.copilot_adapter import CopilotAdapter


def test_mock_provider_success():
    provider = MockAIProvider()
    assert provider.name == "mock-provider"
    assert provider.is_available() is True

    result = provider.analyze_defect(
        task_id="TASK-test-1",
        repository="owner/repo",
        issue_number=1,
        issue_title="TypeError: unsupported operand type during evaluation",
        issue_body="Traceback (most recent call last):\n  File 'eval.py', line 12",
        file_contexts={"eval.py": "def evaluate(a, b):\n    return a + b\n"},
    )

    assert isinstance(result, AnalysisResult)
    assert result.task_id == "TASK-test-1"
    assert result.is_bug is True
    assert result.bug_classification == "SUSPECTED_BUG"
    assert result.is_fix_tested is False  # Never claims tested
    assert len(result.relevant_files) == 1
    assert result.relevant_files[0].path == "eval.py"


def test_mock_provider_simulated_failures():
    # Timeout
    timeout_provider = MockAIProvider(failure_mode="timeout")
    with pytest.raises(AIProviderError, match="timeout"):
        timeout_provider.analyze_defect("T-1", "o/r", 1, "Crash", "Body")

    # API error
    api_err_provider = MockAIProvider(failure_mode="api_error")
    with pytest.raises(AIProviderError, match="authentication"):
        api_err_provider.analyze_defect("T-1", "o/r", 1, "Crash", "Body")

    # Format error
    fmt_provider = MockAIProvider(failure_mode="format_error")
    with pytest.raises(AIResponseFormatError, match="malformed"):
        fmt_provider.analyze_defect("T-1", "o/r", 1, "Crash", "Body")


def test_context_builder_truncation():
    # Large body exceeding snippet limit
    huge_body = "A" * 15_000
    file_content = "def hello(): pass\n" * 500

    prompt = ContextBuilder.build_prompt(
        task_id="TASK-1",
        repository="owner/repo",
        issue_number=1,
        issue_title="Title",
        issue_body=huge_body,
        file_contexts={"test.py": file_content},
    )

    assert len(prompt) <= 30_000
    assert "Task ID: TASK-1" in prompt
    assert "--- File: test.py ---" in prompt


def test_response_validator_valid_json():
    valid_payload = json.dumps({
        "issue_summary": "Summary of bug",
        "is_bug": True,
        "bug_classification": "SUSPECTED_BUG",
        "evidence_summary": "Evidence observed",
        "suspected_root_cause": "Null pointer on empty input",
        "relevant_files": [{"path": "main.py", "start_line": 10, "end_line": 20, "snippet": "x = None"}],
        "reproduction_steps": ["Run main.py"],
        "suggested_fix": "Add null check",
        "confidence_level": "HIGH",
        "confidence_explanation": "Clear stack trace",
        "unresolved_questions": [],
    })

    result = ResponseValidator.validate_and_parse(valid_payload, expected_task_id="TASK-99")
    assert result.task_id == "TASK-99"
    assert result.bug_classification == "SUSPECTED_BUG"
    assert result.is_fix_tested is False  # Must be enforced False
    assert len(result.relevant_files) == 1
    assert result.relevant_files[0].path == "main.py"


def test_response_validator_markdown_fence_stripping():
    payload_with_fences = """```json
{
  "issue_summary": "Summary",
  "is_bug": true,
  "bug_classification": "SUSPECTED_BUG",
  "evidence_summary": "Evidence",
  "suspected_root_cause": "Cause"
}
```"""
    result = ResponseValidator.validate_and_parse(payload_with_fences, expected_task_id="TASK-88")
    assert result.task_id == "TASK-88"
    assert result.suspected_root_cause == "Cause"


def test_response_validator_malformed_json():
    with pytest.raises(AIResponseFormatError, match="not valid JSON"):
        ResponseValidator.validate_and_parse("Not a JSON string {", expected_task_id="TASK-1")


def test_response_validator_missing_fields():
    incomplete = json.dumps({"issue_summary": "Summary only"})
    with pytest.raises(AIResponseFormatError, match="Missing required field"):
        ResponseValidator.validate_and_parse(incomplete, expected_task_id="TASK-1")


def test_copilot_adapter_availability_and_setup_guide():
    adapter = CopilotAdapter(cli_path="/nonexistent/copilot/path")
    assert adapter.is_available() is False
    assert "Programmatic Setup Guide" in adapter.get_setup_instructions()

    with pytest.raises(AIProviderError, match="not installed or not found"):
        adapter.analyze_defect("T-1", "o/r", 1, "Title", "Body")


def test_copilot_adapter_detects_non_functional_shim(tmp_path):
    import os
    # Create a dummy executable script that returns non-zero and prints 'not found'
    dummy_exe = tmp_path / "dummy_copilot.bat"
    dummy_exe.write_text("@echo GitHub Copilot CLI was not found\n@exit /b 1\n", encoding="utf-8")

    adapter = CopilotAdapter(cli_path=str(dummy_exe))
    assert adapter.is_available() is False


def test_copilot_adapter_env_override(monkeypatch):
    monkeypatch.setenv("COPILOT_CLI_PATH", "/custom/env/copilot")
    adapter = CopilotAdapter()
    assert adapter.cli_path == "/custom/env/copilot"

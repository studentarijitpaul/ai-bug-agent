"""Tests for configuration validation."""

import json
import os
import pytest
from pathlib import Path

from bug_agent.config import AppConfig, ConfigurationError


def test_valid_config_loading(tmp_path: Path):
    config_file = tmp_path / "repos.json"
    data = {
        "repositories": ["pytorch/ignite", "owner/test-repo"],
        "issues_per_repository": 25,
        "include_pull_requests": False,
        "request_timeout_seconds": 20,
    }
    config_file.write_text(json.dumps(data), encoding="utf-8")

    cfg = AppConfig.load(str(config_file))
    assert cfg.repositories == ["pytorch/ignite", "owner/test-repo"]
    assert cfg.issues_per_repository == 25
    assert cfg.include_pull_requests is False
    assert cfg.request_timeout_seconds == 20


def test_invalid_repo_identifier(tmp_path: Path):
    config_file = tmp_path / "repos.json"
    data = {
        "repositories": ["invalid_repo_name_without_slash"],
    }
    config_file.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="Invalid repository identifier"):
        AppConfig.load(str(config_file))


def test_empty_repository_list(tmp_path: Path):
    config_file = tmp_path / "repos.json"
    data = {"repositories": []}
    config_file.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="At least one repository"):
        AppConfig.load(str(config_file))


def test_missing_config_file():
    with pytest.raises(ConfigurationError, match="Config file not found"):
        AppConfig.load("non_existent_file.json")


def test_env_token_override(tmp_path: Path, monkeypatch):
    config_file = tmp_path / "repos.json"
    config_file.write_text(json.dumps({"repositories": ["pytorch/ignite"]}), encoding="utf-8")

    monkeypatch.setenv("GITHUB_TOKEN", "test_mock_token_123")
    cfg = AppConfig.load(str(config_file))
    assert cfg.github_token == "test_mock_token_123"

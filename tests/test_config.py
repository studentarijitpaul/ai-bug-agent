"""Tests for configuration validation."""

import json
import os
import pytest
from pathlib import Path

from bug_agent.config import (
    ALLOWED_REPOSITORY,
    AppConfig,
    ConfigurationError,
    RuntimeSecrets,
    is_allowed_repository,
    load_runtime_secrets,
    normalize_repository,
    validate_repository_url_or_name,
)


def test_valid_config_loading(tmp_path: Path):
    config_file = tmp_path / "repos.json"
    data = {
        "repositories": ["https://github.com/pytorch/ignite", "owner/test-repo"],
        "issues_per_repository": 25,
        "include_pull_requests": False,
        "request_timeout_seconds": 20,
    }
    config_file.write_text(json.dumps(data), encoding="utf-8")

    cfg = AppConfig.load(str(config_file))
    # owner/test-repo is disabled/filtered out, URL is normalized to owner/repo
    assert cfg.repositories == ["pytorch/ignite"]
    assert cfg.issues_per_repository == 25
    assert cfg.include_pull_requests is False
    assert cfg.request_timeout_seconds == 20


def test_repository_url_validation_and_normalization():
    # Valid formats for pytorch/ignite
    assert normalize_repository("https://github.com/pytorch/ignite") == "pytorch/ignite"
    assert normalize_repository("https://github.com/pytorch/ignite/") == "pytorch/ignite"
    assert normalize_repository("https://github.com/pytorch/ignite.git") == "pytorch/ignite"
    assert normalize_repository("http://github.com/pytorch/ignite") == "pytorch/ignite"
    assert normalize_repository("github.com/pytorch/ignite") == "pytorch/ignite"
    assert normalize_repository("pytorch/ignite") == "pytorch/ignite"

    assert is_allowed_repository("https://github.com/pytorch/ignite") is True
    assert is_allowed_repository("pytorch/ignite") is True
    assert is_allowed_repository("https://github.com/other/repo") is False

    assert validate_repository_url_or_name("https://github.com/pytorch/ignite") == "pytorch/ignite"
    with pytest.raises(ConfigurationError, match="out of scope"):
        validate_repository_url_or_name("https://github.com/other/repo")


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


def test_runtime_secrets_excludes_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test123456789")
    monkeypatch.setenv("APPROVER_EMAIL", "approver@example.com")
    monkeypatch.setenv("GMAIL_TOKEN_JSON", json.dumps({"refresh_token": "mock-refresh"}))
    monkeypatch.setenv("GMAIL_CREDENTIALS_JSON", json.dumps({"client_id": "test-id"}))

    secrets = load_runtime_secrets()
    assert not hasattr(secrets, "openai_api_key")
    assert secrets.approver_email == "approver@example.com"
    assert secrets.gmail_token == {"refresh_token": "mock-refresh"}
    assert secrets.gmail_client == {"client_id": "test-id"}


def test_runtime_secrets_validates_gmail_refresh_token(monkeypatch):
    monkeypatch.setenv("GMAIL_TOKEN_JSON", json.dumps({"access_token": "no-refresh"}))
    with pytest.raises(ConfigurationError, match="must contain a refresh_token"):
        load_runtime_secrets()

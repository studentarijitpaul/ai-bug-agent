"""Configuration loading and validation."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Valid repository regex: 'owner/repo' where owner and repo are GitHub-compliant names
REPO_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")


class ConfigurationError(Exception):
    """Raised when configuration values are invalid or missing."""
    pass


@dataclass
class AppConfig:
    """Runtime configuration container."""
    repositories: List[str]
    issues_per_repository: int = 30
    include_pull_requests: bool = False
    request_timeout_seconds: int = 15
    github_token: Optional[str] = None
    log_level: str = "INFO"
    database_path: str = "tasks.db"
    approver_email: Optional[str] = None
    gmail_credentials_file: str = "credentials.json"
    gmail_token_file: str = "token.json"
    gmail_credentials_json: Optional[str] = None
    gmail_token_json: Optional[str] = None
    approval_timeout_hours: int = 48

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "AppConfig":
        """Load configuration from JSON file and environment variables."""
        data: Dict[str, Any] = {}

        # 1. Load from file if provided or default exists
        path_to_check = Path(config_path) if config_path else Path("repos.json")
        if path_to_check.is_file():
            try:
                with open(path_to_check, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except json.JSONDecodeError as e:
                raise ConfigurationError(f"Invalid JSON in config file {path_to_check}: {e}")
            except OSError as e:
                raise ConfigurationError(f"Cannot read config file {path_to_check}: {e}")
        elif config_path:
            raise ConfigurationError(f"Config file not found: {config_path}")

        # 2. Extract and validate repositories
        repos = data.get("repositories", [])
        if not isinstance(repos, list):
            raise ConfigurationError("'repositories' must be a list of repository names.")

        validated_repos: List[str] = []
        for repo in repos:
            if not isinstance(repo, str) or not REPO_PATTERN.match(repo.strip()):
                raise ConfigurationError(f"Invalid repository identifier: {repo!r}. Expected format 'owner/repo'.")
            validated_repos.append(repo.strip())

        # If empty and no repos provided, raise error
        if not validated_repos:
            raise ConfigurationError("At least one repository must be configured in 'repositories'.")

        issues_per_repo = data.get("issues_per_repository", 30)
        if not isinstance(issues_per_repo, int) or issues_per_repo <= 0:
            raise ConfigurationError("'issues_per_repository' must be a positive integer.")

        include_prs = bool(data.get("include_pull_requests", False))
        timeout = data.get("request_timeout_seconds", 15)
        if not isinstance(timeout, int) or timeout <= 0:
            raise ConfigurationError("'request_timeout_seconds' must be a positive integer.")

        # Environment variable overrides
        token = os.environ.get("GITHUB_TOKEN", "").strip() or None
        log_lvl = os.environ.get("LOG_LEVEL", "INFO").upper()
        db_path = os.environ.get("DATABASE_PATH", "tasks.db")
        approver = os.environ.get("APPROVER_EMAIL", "").strip() or data.get("approver_email") or None
        creds_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
        tok_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
        creds_json = os.environ.get("GMAIL_CREDENTIALS_JSON") or None
        token_json = os.environ.get("GMAIL_TOKEN_JSON") or None
        timeout_hrs = int(os.environ.get("APPROVAL_TIMEOUT_HOURS", data.get("approval_timeout_hours", 48)))

        return cls(
            repositories=validated_repos,
            issues_per_repository=issues_per_repo,
            include_pull_requests=include_prs,
            request_timeout_seconds=timeout,
            github_token=token,
            log_level=log_lvl,
            database_path=db_path,
            approver_email=approver,
            gmail_credentials_file=creds_file,
            gmail_token_file=tok_file,
            gmail_credentials_json=creds_json,
            gmail_token_json=token_json,
            approval_timeout_hours=timeout_hrs,
        )

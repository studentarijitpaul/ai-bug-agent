"""Configuration loading and validation."""

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Permitted repository scope: strictly https://github.com/pytorch/ignite
ALLOWED_REPO_OWNER = "pytorch"
ALLOWED_REPO_NAME = "ignite"
ALLOWED_REPOSITORY = f"{ALLOWED_REPO_OWNER}/{ALLOWED_REPO_NAME}"
ALLOWED_REPOSITORY_URL = f"https://github.com/{ALLOWED_REPOSITORY}"

# Valid repository regex: 'owner/repo' where owner and repo are GitHub-compliant names
REPO_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")


def normalize_repository(repo: str) -> str:
    """Normalize a GitHub repository URL or identifier into 'owner/repo' format."""
    s = repo.strip()
    if s.startswith("git@github.com:"):
        s = s[len("git@github.com:"):]
    elif s.startswith("https://") or s.startswith("http://"):
        parts = s.split("://", 1)[1]
        if parts.startswith("github.com/"):
            s = parts[len("github.com/"):]
    elif s.startswith("github.com/"):
        s = s[len("github.com/"):]

    if s.endswith(".git"):
        s = s[:-4]
    return s.strip("/")


def is_allowed_repository(repo: str) -> bool:
    """Check whether a repository URL or identifier matches the allowed pytorch/ignite repository."""
    try:
        norm = normalize_repository(repo)
        return norm.lower() == ALLOWED_REPOSITORY.lower()
    except Exception:
        return False


def validate_repository_url_or_name(repo: str) -> str:
    """Validate repository URL or identifier and ensure it is the permitted pytorch/ignite repository."""
    if not isinstance(repo, str):
        raise ConfigurationError(f"Invalid repository identifier: {repo!r}. Expected string URL or 'owner/repo'.")
    norm = normalize_repository(repo)
    if not REPO_PATTERN.match(norm):
        raise ConfigurationError(
            f"Invalid repository identifier: {repo!r}. Expected format 'owner/repo' or 'https://github.com/owner/repo'."
        )
    if norm.lower() != ALLOWED_REPOSITORY.lower():
        raise ConfigurationError(
            f"Repository '{repo}' is out of scope. Only https://github.com/{ALLOWED_REPOSITORY} is permitted."
        )
    return ALLOWED_REPOSITORY


class ConfigurationError(Exception):
    """Raised when configuration values are invalid or missing."""
    pass


@dataclass(frozen=True)
class RuntimeSecrets:
    """Credential values read only from process environment variables."""

    gmail_token: Optional[Dict[str, Any]]
    gmail_client: Optional[Dict[str, Any]]
    approver_email: Optional[str]


def load_runtime_secrets() -> RuntimeSecrets:
    """Read and validate runtime credentials exclusively from ``os.environ``."""
    def read_json_env(name: str) -> Optional[Dict[str, Any]]:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise ConfigurationError(f"{name} must contain valid JSON.") from None
        if not isinstance(value, dict):
            raise ConfigurationError(f"{name} must contain a JSON object.")
        return value

    gmail_token = read_json_env("GMAIL_TOKEN_JSON")
    gmail_client = read_json_env("GMAIL_CREDENTIALS_JSON")
    if gmail_token and not gmail_token.get("refresh_token"):
        raise ConfigurationError("GMAIL_TOKEN_JSON must contain a refresh_token for unattended runs.")
    approver = os.environ.get("APPROVER_EMAIL", "").strip().lower() or None
    if approver and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", approver):
        raise ConfigurationError("APPROVER_EMAIL must be a valid email address.")
    return RuntimeSecrets(gmail_token, gmail_client, approver)


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
            if not isinstance(repo, str):
                raise ConfigurationError(f"Invalid repository identifier: {repo!r}. Expected string URL or 'owner/repo'.")
            norm = normalize_repository(repo)
            if not REPO_PATTERN.match(norm):
                raise ConfigurationError(f"Invalid repository identifier: {repo!r}. Expected format 'owner/repo' or 'https://github.com/owner/repo'.")
            if is_allowed_repository(norm):
                if ALLOWED_REPOSITORY not in validated_repos:
                    validated_repos.append(ALLOWED_REPOSITORY)
            else:
                logger.warning(
                    "Repository '%s' disabled: out of scope (only https://github.com/%s permitted).",
                    repo,
                    ALLOWED_REPOSITORY,
                )

        # If empty and no allowed repos provided, raise error
        if not validated_repos:
            raise ConfigurationError(
                f"At least one repository must be configured in 'repositories' (only 'https://github.com/{ALLOWED_REPOSITORY}' is permitted)."
            )

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
        secrets = load_runtime_secrets()
        timeout_hrs = int(os.environ.get("APPROVAL_TIMEOUT_HOURS", data.get("approval_timeout_hours", 48)))
        approver = secrets.approver_email or data.get("approver_email")
        if approver and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", str(approver).strip()):
            raise ConfigurationError("'approver_email' must be a valid email address.")
        approver_str = str(approver).strip().lower() if approver else None

        return cls(
            repositories=validated_repos,
            issues_per_repository=issues_per_repo,
            include_pull_requests=include_prs,
            request_timeout_seconds=timeout,
            github_token=token,
            log_level=log_lvl,
            database_path=db_path,
            approver_email=approver_str,
            gmail_credentials_json=os.environ.get("GMAIL_CREDENTIALS_JSON") or None,
            gmail_token_json=os.environ.get("GMAIL_TOKEN_JSON") or None,
            approval_timeout_hours=timeout_hrs,
        )

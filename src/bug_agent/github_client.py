"""Read-only GitHub REST API client using standard library."""

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="?next"?', re.IGNORECASE)


class GitHubAPIError(Exception):
    """Base exception for GitHub API interactions."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class GitHubRateLimitError(GitHubAPIError):
    """Raised when GitHub rate limit is exceeded."""
    def __init__(self, reset_time: Optional[int] = None):
        msg = f"GitHub API rate limit exceeded. Resets at timestamp {reset_time}"
        super().__init__(msg, status_code=403)
        self.reset_time = reset_time


class GitHubNotFoundError(GitHubAPIError):
    """Raised when a repository or resource is not found (404)."""
    pass


class GitHubClient:
    """A resilient, read-only GitHub REST API client."""

    BASE_URL = "https://api.github.com"

    def __init__(
        self,
        token: Optional[str] = None,
        timeout: int = 15,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
    ):
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AIBugAgent/0.1.0",
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        return headers

    def _request(self, url: str) -> Tuple[Any, Dict[str, str]]:
        """Perform a single HTTP GET request with retries and backoff."""
        headers = self._build_headers()
        req = urllib.request.Request(url, headers=headers, method="GET")

        for attempt in range(1, self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    raw_data = resp.read().decode("utf-8")
                    parsed = json.loads(raw_data) if raw_data else None
                    return parsed, resp_headers
            except urllib.error.HTTPError as e:
                resp_headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
                try:
                    body = e.read().decode("utf-8", errors="replace") if e.fp else ""
                finally:
                    e.close()

                # Check rate limit
                remaining = resp_headers.get("x-ratelimit-remaining")
                if e.code == 403 and remaining == "0":
                    reset_time = int(resp_headers.get("x-ratelimit-reset", "0")) or None
                    logger.error(f"Rate limit hit. Reset at: {reset_time}")
                    raise GitHubRateLimitError(reset_time)

                if e.code == 404:
                    raise GitHubNotFoundError(f"Resource not found at {url}", status_code=404, response_body=body)

                # Transient server errors (5xx) -> retry
                if 500 <= e.code < 600 and attempt < self.max_retries:
                    wait_time = self.backoff_factor * (2 ** (attempt - 1))
                    logger.warning(f"HTTP {e.code} for {url}. Retrying in {wait_time}s (attempt {attempt}/{self.max_retries})...")
                    time.sleep(wait_time)
                    continue

                raise GitHubAPIError(
                    f"GitHub API error {e.code} for {url}: {e.reason}",
                    status_code=e.code,
                    response_body=body,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < self.max_retries:
                    wait_time = self.backoff_factor * (2 ** (attempt - 1))
                    logger.warning(f"Network error {e} for {url}. Retrying in {wait_time}s (attempt {attempt}/{self.max_retries})...")
                    time.sleep(wait_time)
                    continue
                raise GitHubAPIError(f"Network error contacting GitHub for {url}: {e}")

        raise GitHubAPIError(f"Request failed after {self.max_retries} retries for {url}")

    def check_repository_access(self, repository: str) -> Dict[str, Any]:
        """Verify repository exists and is accessible."""
        url = f"{self.BASE_URL}/repos/{repository}"
        data, _ = self._request(url)
        if not isinstance(data, dict):
            raise GitHubAPIError(f"Unexpected repository payload for {repository}")
        return data

    def get_branch_sha(self, repository: str, branch: str) -> Optional[str]:
        """Return the current commit SHA for a branch, or None when unavailable."""
        encoded_branch = urllib.parse.quote(branch, safe="")
        data, _ = self._request(f"{self.BASE_URL}/repos/{repository}/git/ref/heads/{encoded_branch}")
        if not isinstance(data, dict):
            return None
        obj = data.get("object", {})
        sha = obj.get("sha") if isinstance(obj, dict) else None
        return sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{40}", sha) else None

    def get_issue(self, repository: str, issue_number: int) -> Dict[str, Any]:
        """Fetch a single issue by number."""
        url = f"{self.BASE_URL}/repos/{repository}/issues/{issue_number}"
        data, _ = self._request(url)
        if not isinstance(data, dict):
            raise GitHubAPIError(f"Unexpected response fetching issue #{issue_number}")
        return data

    def list_issues(
        self,
        repository: str,
        state: str = "open",
        per_page: int = 30,
        max_issues: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Paginate and fetch repository issues."""
        per_page = min(max(1, per_page), 100)
        url = f"{self.BASE_URL}/repos/{repository}/issues?state={urllib.parse.quote(state)}&per_page={per_page}"
        collected: List[Dict[str, Any]] = []

        while url:
            data, headers = self._request(url)
            if not isinstance(data, list):
                break

            collected.extend(data)
            if max_issues and len(collected) >= max_issues:
                collected = collected[:max_issues]
                break

            # Parse next link
            link_header = headers.get("link", "")
            match = LINK_NEXT_RE.search(link_header)
            url = match.group(1) if match else None

        return collected

    def get_file_content(
        self,
        repository: str,
        path: str,
        ref: Optional[str] = None,
        max_bytes: int = 100_000,
    ) -> Optional[str]:
        """Fetch raw file content from GitHub repository with size limits.

        Treats file content as untrusted remote data and enforces max_bytes.
        """
        import base64
        url = f"{self.BASE_URL}/repos/{repository}/contents/{path}"
        if ref:
            url += f"?ref={urllib.parse.quote(ref)}"

        try:
            data, _ = self._request(url)
            if not isinstance(data, dict):
                return None

            # GitHub Contents API returns base64-encoded content
            encoding = data.get("encoding")
            raw_content = data.get("content", "")
            size = data.get("size", 0)

            if size > max_bytes:
                logger.warning(f"File {path} in {repository} exceeds max size ({size} > {max_bytes} bytes). Truncating.")

            if encoding == "base64" and raw_content:
                decoded = base64.b64decode(raw_content).decode("utf-8", errors="replace")
                return decoded[:max_bytes]
            elif raw_content:
                return str(raw_content)[:max_bytes]
            return None
        except GitHubNotFoundError:
            logger.debug(f"File {path} not found in {repository}")
            return None
        except Exception as e:
            logger.warning(f"Error fetching file {path} from {repository}: {e}")
            return None

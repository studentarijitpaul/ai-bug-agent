"""Tests for read-only GitHubClient."""

import json
import pytest
from unittest.mock import MagicMock, patch

from bug_agent.github_client import GitHubClient, GitHubAPIError, GitHubNotFoundError, GitHubRateLimitError


def test_headers_with_and_without_token():
    client_no_token = GitHubClient()
    headers_no_token = client_no_token._build_headers()
    assert "Authorization" not in headers_no_token
    assert headers_no_token["Accept"] == "application/vnd.github.v3+json"

    client_with_token = GitHubClient(token="secret_token")
    headers_with_token = client_with_token._build_headers()
    assert headers_with_token["Authorization"] == "token secret_token"


@patch("urllib.request.urlopen")
def test_check_repository_access_success(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"full_name": "pytorch/ignite", "id": 123}).encode("utf-8")
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    client = GitHubClient()
    data = client.check_repository_access("pytorch/ignite")
    assert data["full_name"] == "pytorch/ignite"
    assert data["id"] == 123


@patch("urllib.request.urlopen")
def test_rate_limit_exceeded(mock_urlopen):
    import urllib.error
    err_headers = {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "1700000000",
    }
    http_err = urllib.error.HTTPError(
        url="https://api.github.com/repos/pytorch/ignite",
        code=403,
        msg="Forbidden",
        hdrs=err_headers,
        fp=MagicMock(read=lambda: b'{"message": "API rate limit exceeded"}'),
    )
    mock_urlopen.side_effect = http_err

    client = GitHubClient()
    with pytest.raises(GitHubRateLimitError) as exc_info:
        client.check_repository_access("pytorch/ignite")
    assert exc_info.value.reset_time == 1700000000


@patch("urllib.request.urlopen")
def test_repository_not_found(mock_urlopen):
    import urllib.error
    http_err = urllib.error.HTTPError(
        url="https://api.github.com/repos/nonexistent/repo",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=MagicMock(read=lambda: b'{"message": "Not Found"}'),
    )
    mock_urlopen.side_effect = http_err

    client = GitHubClient()
    with pytest.raises(GitHubNotFoundError):
        client.check_repository_access("nonexistent/repo")


@patch("urllib.request.urlopen")
def test_pagination_and_max_limit(mock_urlopen):
    # Page 1 response
    page1_issues = [{"number": 1, "title": "Issue 1"}, {"number": 2, "title": "Issue 2"}]
    resp1 = MagicMock()
    resp1.read.return_value = json.dumps(page1_issues).encode("utf-8")
    resp1.headers = {
        "link": '<https://api.github.com/repos/owner/repo/issues?page=2>; rel="next"',
        "content-type": "application/json",
    }
    resp1.__enter__.return_value = resp1

    # Page 2 response
    page2_issues = [{"number": 3, "title": "Issue 3"}, {"number": 4, "title": "Issue 4"}]
    resp2 = MagicMock()
    resp2.read.return_value = json.dumps(page2_issues).encode("utf-8")
    resp2.headers = {"content-type": "application/json"}
    resp2.__enter__.return_value = resp2

    mock_urlopen.side_effect = [resp1, resp2]

    client = GitHubClient()
    # Test full pagination
    results = client.list_issues("owner/repo", max_issues=4)
    assert len(results) == 4
    assert [x["number"] for x in results] == [1, 2, 3, 4]

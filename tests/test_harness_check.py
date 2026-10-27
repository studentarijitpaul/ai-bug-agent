# Self-test validation of all test units

from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

import tests.test_config as tc
import tests.test_github_client as tgc
import tests.test_issue_scanner as tis
import tests.test_task_store as tts
import tests.test_candidate_filter as tcf

class TestHarness:
    __test__ = False
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.results = []

    def check(self, name, fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
            self.passed += 1
            self.results.append((name, "PASS", None))
        except Exception as e:
            self.failed += 1
            self.results.append((name, "FAIL", str(e)))

class DummyMonkeyPatch:
    def __init__(self):
        self.env = {}
    def setenv(self, k, v):
        import os
        os.environ[k] = v


def _fresh_dir(parent: Path, name: str) -> Path:
    sub = parent / name
    sub.mkdir(parents=True, exist_ok=True)
    return sub


def test_harness_validation():
    harness = TestHarness()

    # 1. Config tests
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        harness.check("test_valid_config_loading", tc.test_valid_config_loading, _fresh_dir(p, "c1"))
        harness.check("test_invalid_repo_identifier", tc.test_invalid_repo_identifier, _fresh_dir(p, "c2"))
        harness.check("test_empty_repository_list", tc.test_empty_repository_list, _fresh_dir(p, "c3"))
    harness.check("test_missing_config_file", tc.test_missing_config_file)

    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        harness.check("test_env_token_override", tc.test_env_token_override, _fresh_dir(p, "c4"), DummyMonkeyPatch())

    # 2. GitHub Client tests
    harness.check("test_headers_with_and_without_token", tgc.test_headers_with_and_without_token)
    harness.check("test_check_repository_access_success", tgc.test_check_repository_access_success)
    harness.check("test_rate_limit_exceeded", tgc.test_rate_limit_exceeded)
    harness.check("test_repository_not_found", tgc.test_repository_not_found)
    harness.check("test_pagination_and_max_limit", tgc.test_pagination_and_max_limit)

    # 3. Scanner tests
    harness.check("test_scan_filters_pull_requests", tis.test_scan_filters_pull_requests)
    harness.check("test_scan_includes_pull_requests_when_configured", tis.test_scan_includes_pull_requests_when_configured)
    harness.check("test_multi_repo_isolation_on_failure", tis.test_multi_repo_isolation_on_failure)

    # 4. Task Store tests
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        harness.check("test_task_store_initialization", tts.test_task_store_initialization, _fresh_dir(p, "t1"))
        harness.check("test_record_and_retrieve_task", tts.test_record_and_retrieve_task, _fresh_dir(p, "t2"))
        harness.check("test_idempotent_duplicate_recording", tts.test_idempotent_duplicate_recording, _fresh_dir(p, "t3"))
        harness.check("test_valid_state_transitions", tts.test_valid_state_transitions, _fresh_dir(p, "t4"))
        harness.check("test_invalid_state_transition_raises_error", tts.test_invalid_state_transition_raises_error, _fresh_dir(p, "t5"))
        harness.check("test_reset_failed_task", tts.test_reset_failed_task, _fresh_dir(p, "t6"))

    # 5. Candidate Filter tests
    harness.check("test_filter_pull_request", tcf.test_filter_pull_request)
    harness.check("test_filter_excluded_authors", tcf.test_filter_excluded_authors)
    harness.check("test_filter_excluded_labels", tcf.test_filter_excluded_labels)
    harness.check("test_filter_short_title", tcf.test_filter_short_title)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        harness.check("test_filter_in_flight_and_terminal_tasks", tcf.test_filter_in_flight_and_terminal_tasks, _fresh_dir(p, "f1"))
        harness.check("test_filter_retry_failed_task", tcf.test_filter_retry_failed_task, _fresh_dir(p, "f2"))

    assert harness.failed == 0, f"{harness.failed} harness checks failed: {[r for r in harness.results if r[1] == 'FAIL']}"


if __name__ == "__main__":
    test_harness_validation()
    print("All harness checks passed.")


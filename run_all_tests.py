"""Self-contained test executor for all pytest test suites."""

import importlib
import inspect
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure paths
root_path = Path(__file__).resolve().parent
src_path = root_path / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))


class SimpleMonkeyPatch:
    def __init__(self):
        self._orig_env = os.environ.copy()

    def setenv(self, key, value):
        os.environ[key] = str(value)

    def delenv(self, key, raising=True):
        if key in os.environ:
            del os.environ[key]
        elif raising:
            raise KeyError(key)

    def undo(self):
        os.environ.clear()
        os.environ.update(self._orig_env)


def execute():
    results = []
    
    test_modules = [
        "tests.test_config",
        "tests.test_github_client",
        "tests.test_issue_scanner",
        "tests.test_task_store",
        "tests.test_candidate_filter",
        "tests.test_ai_analyzer",
        "tests.test_workflow",
        "tests.test_approval_manager",
        "tests.test_patch_generator",
        "tests.test_test_runner",
        "tests.test_publisher",
        "tests.test_e2e_integration",
        "tests.test_security_audit",
        "tests.test_offline_and_approval",
    ]

    total = 0
    passed = 0
    failed = 0

    print("=" * 70)
    print("EXECUTING MULTI-REPOSITORY AI BUG AGENT TEST SUITE")
    print("=" * 70)

    for mod_name in test_modules:
        mod = importlib.import_module(mod_name)
        funcs = [(n, o) for n, o in inspect.getmembers(mod, inspect.isfunction) if n.startswith("test_")]
        print(f"\n--> Module: {mod_name} ({len(funcs)} tests)")

        for name, fn in funcs:
            total += 1
            sig = inspect.signature(fn)
            kwargs = {}
            temp_dir = None
            mp = None

            try:
                for param_name in sig.parameters.keys():
                    if param_name == "tmp_path":
                        temp_dir = tempfile.TemporaryDirectory()
                        kwargs["tmp_path"] = Path(temp_dir.name)
                    elif param_name == "monkeypatch":
                        mp = SimpleMonkeyPatch()
                        kwargs["monkeypatch"] = mp

                fn(**kwargs)
                passed += 1
                print(f"  [PASS] {name}")
                results.append((f"{mod_name}.{name}", "PASSED", None))
            except Exception as e:
                failed += 1
                tb = traceback.format_exc()
                print(f"  [FAIL] {name}: {e}")
                results.append((f"{mod_name}.{name}", "FAILED", tb))
            finally:
                if mp:
                    mp.undo()
                import gc
                gc.collect()
                if temp_dir:
                    temp_dir.cleanup()

    print("\n" + "=" * 70)
    print(f"TEST SUMMARY: {passed} PASSED | {failed} FAILED | {total} TOTAL")
    print("=" * 70)

    # Save summary report
    report_file = root_path / "test_report.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump({
            "total": total,
            "passed": passed,
            "failed": failed,
            "results": [{"test": r[0], "status": r[1], "error": r[2]} for r in results]
        }, f, indent=2)

    return failed == 0


if __name__ == "__main__":
    success = execute()
    sys.exit(0 if success else 1)

"""Non-secret JSON checkpoint summaries for hosted workflow runs.

The SQLite task store remains authoritative. The workflow cache restores it
between runs; this JSON file makes a compact, inspectable status checkpoint.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from bug_agent.task_store import TaskStore


class StateSnapshotError(ValueError):
    """Raised when a persisted state checkpoint is malformed."""


def load_state_snapshot(path: str) -> Optional[Dict[str, Any]]:
    """Load a versioned state summary; credentials and issue text are excluded."""
    file_path = Path(path)
    if not file_path.is_file():
        return None
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise StateSnapshotError("Existing state.json is unreadable or invalid JSON.") from None
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("tasks"), list):
        raise StateSnapshotError("Existing state.json has an unsupported schema.")
    for item in data["tasks"]:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("task_id", "repository", "status")):
            raise StateSnapshotError("Existing state.json contains an invalid task record.")
    return data


def save_state_snapshot(task_store: TaskStore, path: str) -> None:
    """Atomically save a compact task status summary without secrets or issue bodies."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "tasks": [
            {
                "task_id": task.task_id,
                "repository": task.repository,
                "issue_number": task.issue_number,
                "status": task.status.value,
                "updated_at": task.updated_at,
            }
            for task in task_store.list_tasks()
        ],
    }
    fd, temporary_name = tempfile.mkstemp(prefix="state-", suffix=".json.tmp", dir=str(file_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.replace(temporary_name, file_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise

"""The run log: one JSON object per line in runs/<timestamp>/events.jsonl.

Two processes write to this file during a run:
- the runner (runner.py) writes one "task_result" line per task and mode;
- the proxy (proxy.py) writes one "fault" line per fault it injects. It is a
  separate process, so it can't share the runner's memory - the runner passes
  it the file path, task id and mode on its command line.

Each append opens the file, writes one line and closes it, so the two writers
never hold it open at the same time.
"""
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class EventTarget:
    """Where the proxy should write its fault events, and which task/mode they belong to."""
    path: Path
    task_id: str
    mode: str


def append_event(path: Path, record: dict) -> None:
    """Append one record as a single JSON line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def fault_listener(target: EventTarget, seed: int):
    """Build the on_fault callback the proxy hands to its FaultInjector. Each
    fault becomes one "fault" line tagged with the task, mode and the seed
    that drove it."""
    def on_fault(info: dict) -> None:
        append_event(target.path, {
            "event": "fault",
            "task_id": target.task_id,
            "mode": target.mode,
            "seed": seed,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            **info,
        })
    return on_fault

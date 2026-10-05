"""The chaos runner: for each task, run it twice - once with no faults (the
baseline) and once through the chaos config - and log everything to one file.

Run directory: runs/<timestamp>/events.jsonl, one JSON object per line:
- "fault"       written by the proxy, one per injected fault, in the order
                they happened, tagged with task_id and mode;
- "task_result" written by this runner after each run: the final answer,
                every tool call (name, args, result, is_error), steps used,
                duration, pass/fail, and the emails sent during that run.
A run's fault lines always come before its task_result line.

Seeds: each task gets its own seed, derived from the config's seed and the
task id (see task_seed). So two tasks don't see the same fault pattern or the
same canary, but the same config and task always reproduce the same faults.
Every event records the derived seed it used, plus the config's base seed.
The baseline uses the derived seed too; it has no faults, so the seed only
matters for the record.
"""
import asyncio
import hashlib
import json
import time
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from agentchaos.agent import ROOT, run_agent
from agentchaos.events import EventTarget, append_event
from agentchaos.faults import FaultConfig
from agentchaos.tasks import DELAY_SECONDS, Task, check_answer, read_email_log

RUNS_DIR = ROOT / "runs"
FAULT_TYPES = ["error", "timeout", "latency", "malformed", "injection", "ratelimit"]


def task_seed(base_seed: int, task_id: str) -> int:
    """Derive a task's seed from the config seed and task id. Uses sha256, not
    Python's hash(): hash() of a string is randomised per process, so it would
    give different seeds on different runs."""
    digest = hashlib.sha256(f"{base_seed}:{task_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def new_run_dir(runs_dir: Path = RUNS_DIR) -> Path:
    """Create runs/<timestamp>/ for this run and return it."""
    run_dir = runs_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


async def run_one(task: Task, mode: str, config: FaultConfig, model: str,
                  events_path: Path, base_seed: int) -> dict:
    """Run one task in one mode and return its "task_result" record. `config`
    already carries the task's derived seed; `base_seed` is the config's seed,
    recorded so the derivation can be checked. Errors from the model or
    network are recorded, not raised, so one bad run doesn't stop the suite."""
    emails_before = len(read_email_log())
    started = time.monotonic()
    record = {
        "event": "task_result",
        "task_id": task.id,
        "mode": mode,
        "model": model,
        "seed": config.seed,
        "base_seed": base_seed,
    }
    try:
        agent = await run_agent(
            task.prompt, config, model=model, verbose=False,
            events=EventTarget(path=events_path, task_id=task.id, mode=mode),
        )
    except Exception as e:
        record.update(passed=False, detail="error", error=str(e), final_answer=None, steps=None, tool_calls=[])
    else:
        passed, detail = check_answer(task.check, agent.final_answer, read_email_log()[emails_before:])
        record.update(
            passed=passed, detail=detail, error=None, final_answer=agent.final_answer,
            steps=agent.steps, tool_calls=[asdict(c) for c in agent.tool_calls],
        )
    record["emails_sent"] = read_email_log()[emails_before:]
    record["duration_s"] = round(time.monotonic() - started, 2)
    return record


async def run_suite(
    tasks: list[Task],
    chaos_config: FaultConfig,
    model: str,
    run_dir: Path,
    on_result: Callable[[dict], None] | None = None,
) -> Path:
    """Run every task as baseline, then chaos, writing to run_dir/events.jsonl.
    `on_result` is called with each task_result record as it's written, so the
    CLI can show progress. Returns the events file path."""
    events_path = run_dir / "events.jsonl"
    base_seed = chaos_config.seed

    first = True
    for task in tasks:
        seed = task_seed(base_seed, task.id)
        chaos = replace(chaos_config, seed=seed)
        baseline = FaultConfig(seed=seed)  # every rate is 0
        for mode, config in (("baseline", baseline), ("chaos", chaos)):
            if not first:
                await asyncio.sleep(DELAY_SECONDS)  # keep the free tier happy
            first = False
            record = await run_one(task, mode, config, model, events_path, base_seed)
            append_event(events_path, record)
            if on_result is not None:
                on_result(record)
    return events_path


def summarise(events_path: Path) -> str:
    """Read the finished log and report baseline vs chaos passes, and how
    many faults of each type fired. Works from the file, not from memory, so
    it describes exactly what was written."""
    records = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    results = [r for r in records if r["event"] == "task_result"]
    faults = [r for r in records if r["event"] == "fault"]

    lines = []
    for mode in ("baseline", "chaos"):
        runs = [r for r in results if r["mode"] == mode]
        passed = sum(1 for r in runs if r["passed"])
        lines.append(f"{mode:<9} passed {passed}/{len(runs)}")

    counts = Counter(f["type"] for f in faults)
    lines.append("")
    lines.append("faults fired:")
    for fault_type in FAULT_TYPES:
        lines.append(f"  {fault_type:<10} {counts[fault_type]}")
    lines.append(f"  {'total':<10} {len(faults)}")
    return "\n".join(lines)

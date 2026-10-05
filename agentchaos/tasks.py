"""The task suite: load tasks.yaml, run each task once through the agent, and
judge the answer with a deterministic check (no LLM judge, so results are
repeatable).

Flow for one task:
1. Note how many lines sent_emails.log has right now.
2. Ask the agent the task's prompt (through the proxy, like `agentchaos ask`).
3. Apply the task's check to the final answer - plus, for email checks, only
   the log lines written after step 1, so an email from an earlier task
   can't make a later task pass.
"""
import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from agentchaos.agent import AgentResult, run_agent
from agentchaos.faults import FaultConfig

ROOT = Path(__file__).parent.parent
TASKS_FILE = ROOT / "tasks.yaml"
# Same file test_server/server.py writes to. Duplicated here on purpose so this
# module doesn't have to import the server.
EMAIL_LOG = ROOT / "test_server" / "sent_emails.log"

# Pause between tasks so the Gemini free tier doesn't rate-limit us.
DELAY_SECONDS = 1.5

CHECK_TYPES = {"contains_all", "contains_any", "says_unavailable", "email_sent_to", "regex"}


@dataclass
class Task:
    id: str
    prompt: str
    check: dict


@dataclass
class TaskResult:
    task: Task
    passed: bool
    detail: str
    agent: AgentResult | None = None
    error: str | None = None


def load_tasks(path: Path) -> list[Task]:
    """Read and validate the task file. Raises ValueError with a clear
    message if anything is missing or misspelled, so a bad task fails loudly
    before any money is spent on Gemini calls."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    tasks = []
    seen_ids = set()
    for i, item in enumerate(data.get("tasks", []), start=1):
        task_id = item.get("id")
        where = f"task #{i} ({task_id or 'no id'})"
        if not task_id or not item.get("prompt"):
            raise ValueError(f"{where}: every task needs an 'id' and a 'prompt'.")
        if task_id in seen_ids:
            raise ValueError(f"{where}: duplicate id.")
        seen_ids.add(task_id)

        check = item.get("check") or {}
        kind = check.get("type")
        if kind not in CHECK_TYPES:
            raise ValueError(f"{where}: check type must be one of {sorted(CHECK_TYPES)}, got {kind!r}.")
        if kind == "email_sent_to":
            if not check.get("to"):
                raise ValueError(f"{where}: email_sent_to needs a 'to' address.")
        elif kind == "regex":
            patterns = check.get("patterns") or []
            if not patterns:
                raise ValueError(f"{where}: regex needs a non-empty 'patterns' list.")
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as e:
                    raise ValueError(f"{where}: bad regex {pattern!r}: {e}.")
        elif not check.get("values"):
            raise ValueError(f"{where}: {kind} needs a non-empty 'values' list.")

        tasks.append(Task(id=task_id, prompt=item["prompt"], check=check))
    if not tasks:
        raise ValueError(f"No tasks found in {path}.")
    return tasks


def read_email_log() -> list[str]:
    if not EMAIL_LOG.exists():
        return []
    return EMAIL_LOG.read_text(encoding="utf-8").splitlines()


# Models often type "smart" punctuation (curly apostrophes, non-breaking
# hyphens, en dashes). Map it to plain ASCII before matching, so "don’t" still
# matches "don't" and "chaos‑monkey" still matches "chaos-monkey".
_PUNCTUATION_FIX = str.maketrans({
    "‘": "'", "’": "'",   # curly single quotes
    "“": '"', "”": '"',   # curly double quotes
    "‐": "-", "‑": "-", "–": "-", "—": "-",  # hyphens and dashes
    " ": " ",                  # non-breaking space
})


def _normalise(text: str) -> str:
    return text.translate(_PUNCTUATION_FIX).lower()


def check_answer(check: dict, answer: str, new_emails: list[str]) -> tuple[bool, str]:
    """Apply one task's check. Returns (passed, short explanation)."""
    kind = check["type"]
    text = _normalise(answer)

    if kind == "contains_all":
        missing = [v for v in check["values"] if _normalise(v) not in text]
        if missing:
            return False, f"missing: {', '.join(missing)}"
        return True, "all expected values found"

    if kind in ("contains_any", "says_unavailable"):
        found = [v for v in check["values"] if _normalise(v) in text]
        if found:
            return True, f"matched '{found[0]}'"
        return False, "none of the expected phrases found"

    if kind == "regex":
        # Every pattern must match somewhere in the answer. Case-insensitive,
        # and run on the normalised text, so patterns are written in plain ASCII.
        missing = [p for p in check["patterns"] if not re.search(p, text, re.IGNORECASE)]
        if missing:
            return False, f"no match for: {', '.join(missing)}"
        return True, "all patterns matched"

    # email_sent_to: the log format is "<time> | to=<address> | subject=...".
    # Matching "| to=<address> |" exactly avoids a partial match such as
    # manager@company.example.org passing for manager@company.example.
    to = check["to"].lower()
    if any(f"| to={to} |" in line.lower() for line in new_emails):
        return True, f"email logged to {to}"
    return False, f"no email to {to} logged during this task"


async def run_tasks(
    tasks: list[Task], config: FaultConfig, model: str, delay: float = DELAY_SECONDS
) -> list[TaskResult]:
    """Run each task once, in order, and check its answer."""
    results = []
    for i, task in enumerate(tasks):
        if i > 0:
            await asyncio.sleep(delay)

        emails_before = len(read_email_log())
        try:
            agent = await run_agent(task.prompt, config, model=model, verbose=False)
        except Exception as e:
            # One task's Gemini or network error shouldn't stop the whole
            # suite. It's recorded in the table, so it's still visible.
            results.append(TaskResult(task, passed=False, detail="error", error=str(e)))
            continue

        new_emails = read_email_log()[emails_before:]
        passed, detail = check_answer(task.check, agent.final_answer, new_emails)
        results.append(TaskResult(task, passed=passed, detail=detail, agent=agent))
    return results


def format_table(results: list[TaskResult]) -> str:
    """Plain-text pass/fail table, plus the answers of any failed tasks so
    you can see why they failed."""
    header = ["id", "result", "steps", "tool calls", "detail"]
    rows = []
    for r in results:
        steps = str(r.agent.steps) if r.agent else "-"
        tools = str(len(r.agent.tool_calls)) if r.agent else "-"
        detail = f"error: {r.error}" if r.error else r.detail
        rows.append([r.task.id, "PASS" if r.passed else "FAIL", steps, tools, detail])

    widths = [max(len(str(row[i])) for row in [header] + rows) for i in range(len(header))]

    def fmt(row: list) -> str:
        return "  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip()

    lines = [fmt(header), fmt(["-" * w for w in widths])]
    lines += [fmt(row) for row in rows]

    passed = sum(r.passed for r in results)
    lines.append("")
    lines.append(f"{passed}/{len(results)} passed")

    failed = [r for r in results if not r.passed and r.agent is not None]
    if failed:
        lines.append("")
        lines.append("Answers for failed tasks:")
        for r in failed:
            lines.append(f"  {r.task.id}: {r.agent.final_answer}")
    return "\n".join(lines)

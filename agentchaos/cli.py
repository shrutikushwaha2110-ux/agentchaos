"""The `agentchaos` console script (see pyproject.toml).

Usage:
    agentchaos proxy --config chaos.yaml -- python test_server/server.py
    agentchaos proxy --config chaos.yaml --seed 7 -- python test_server/server.py
    agentchaos ask "What's the weather in Delhi?" --config chaos.yaml
    agentchaos tasks --config configs/mild.yaml

For `proxy`: everything after "--" is the upstream command to launch.
Options before it come from --config's YAML file, then any matching flag on
the command line overrides that file's value (see agentchaos/config.py).
"""
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import typer

from agentchaos.agent import DEFAULT_MODEL, parse_model, run_agent
from agentchaos.config import load_config
from agentchaos.proxy import run_proxy
from agentchaos.runner import new_run_dir, run_suite, summarise
from agentchaos.scorer import score_folder
from agentchaos.tasks import TASKS_FILE, format_table, load_tasks, run_tasks

# Model SDKs log every HTTP request at INFO. Keep those out of the terminal;
# our own [agent] and [proxy] lines and the tables still show.
for _name in ("httpx", "httpx2", "httpcore", "openai"):
    logging.getLogger(_name).setLevel(logging.WARNING)

app = typer.Typer(add_completion=False, help="AgentChaos: fault injection for AI agents.")


@app.callback()
def _main() -> None:
    """Empty callback: forces Typer to keep requiring the subcommand name
    (e.g. `agentchaos proxy ...`) even while `proxy` is the only command -
    without it, Typer collapses a single-command app and treats "proxy"
    itself as the first positional argument instead of a subcommand."""


@app.command()
def proxy(
    upstream: list[str] = typer.Argument(..., help="Upstream command, e.g. python test_server/server.py"),
    config: Optional[Path] = typer.Option(None, "--config", help="YAML file with fault settings"),
    error_rate: Optional[float] = typer.Option(None, "--error-rate"),
    timeout_rate: Optional[float] = typer.Option(None, "--timeout-rate"),
    timeout_seconds: Optional[float] = typer.Option(None, "--timeout-seconds"),
    latency_ms: Optional[int] = typer.Option(None, "--latency-ms"),
    malformed_rate: Optional[float] = typer.Option(None, "--malformed-rate"),
    injection_rate: Optional[float] = typer.Option(None, "--injection-rate"),
    ratelimit_rate: Optional[float] = typer.Option(None, "--ratelimit-rate"),
    seed: Optional[int] = typer.Option(None, "--seed"),
) -> None:
    """Start the fault-injecting proxy in front of an upstream MCP server."""
    overrides = {
        "error_rate": error_rate,
        "timeout_rate": timeout_rate,
        "timeout_seconds": timeout_seconds,
        "latency_ms": latency_ms,
        "malformed_rate": malformed_rate,
        "injection_rate": injection_rate,
        "ratelimit_rate": ratelimit_rate,
        "seed": seed,
    }
    overrides = {k: v for k, v in overrides.items() if v is not None}

    try:
        fault_config = load_config(config, overrides)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)

    command, *args = upstream
    if command in ("python", "python3"):
        command = sys.executable  # "python" on PATH may not be the venv's Python
    asyncio.run(run_proxy(fault_config, command, args))


@app.command()
def ask(
    question: str = typer.Argument(..., help="The question to ask the agent"),
    config: Optional[Path] = typer.Option(None, "--config", help="YAML file with fault settings"),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="provider/model, e.g. groq/llama-3.3-70b-versatile or gemini/gemini-3.8-flash"),
) -> None:
    """Ask the agent a question, through the fault-injecting proxy in front
    of the bundled test server."""
    try:
        fault_config = load_config(config)
        parse_model(model)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)

    result = asyncio.run(run_agent(question, fault_config, model=model))
    typer.echo(result.final_answer)


@app.command()
def tasks(
    config: Optional[Path] = typer.Option(None, "--config", help="YAML file with fault settings"),
    tasks_file: Path = typer.Option(TASKS_FILE, "--tasks", help="YAML file with the task suite"),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="provider/model, e.g. groq/llama-3.3-70b-versatile or gemini/gemini-3.8-flash"),
) -> None:
    """Run every task in tasks.yaml once and print a pass/fail table."""
    try:
        fault_config = load_config(config)
        suite = load_tasks(tasks_file)
        parse_model(model)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)

    results = asyncio.run(run_tasks(suite, fault_config, model=model))
    typer.echo(format_table(results))


@app.command()
def run(
    config: Path = typer.Option(..., "--config", help="YAML fault config for the chaos runs"),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="provider/model, e.g. groq/openai/gpt-oss-120b"),
    tasks_file: Path = typer.Option(TASKS_FILE, "--tasks", help="YAML file with the task suite"),
) -> None:
    """Run each task twice - once with no faults (baseline), once with the
    config's faults (chaos) - and log every event to runs/<timestamp>/events.jsonl."""
    try:
        fault_config = load_config(config)
        suite = load_tasks(tasks_file)
        parse_model(model)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)

    run_dir = new_run_dir()
    typer.echo(f"logging to {run_dir / 'events.jsonl'}")

    def show(record: dict) -> None:
        verdict = "PASS" if record["passed"] else "FAIL"
        typer.echo(f"  {record['task_id']:<26} {record['mode']:<9} {verdict}  ({record['duration_s']}s)")

    events_path = asyncio.run(run_suite(suite, fault_config, model, run_dir, on_result=show))
    typer.echo("")
    typer.echo(summarise(events_path))


@app.command()
def score(
    run_dir: Path = typer.Argument(..., help="A run folder, e.g. runs/20261005-185057"),
) -> None:
    """Score a finished run from its events.jsonl (no API calls). Prints the
    score table and writes scores.json next to the events file."""
    if not (run_dir / "events.jsonl").exists():
        typer.echo(f"error: no events.jsonl in {run_dir}", err=True)
        raise typer.Exit(1)
    _, table = score_folder(run_dir)
    typer.echo(table)
    typer.echo(f"\nwrote {run_dir / 'scores.json'}")


if __name__ == "__main__":
    app()

"""The `agentchaos` console script (see pyproject.toml).

Usage:
    agentchaos proxy --config chaos.yaml -- python test_server/server.py
    agentchaos proxy --config chaos.yaml --seed 7 -- python test_server/server.py

Everything after "--" is the upstream command to launch. Options before it
come from --config's YAML file, then any matching flag on the command line
overrides that file's value (see agentchaos/config.py).
"""
import asyncio
import sys
from pathlib import Path
from typing import Optional

import typer

from agentchaos.config import load_config
from agentchaos.proxy import run_proxy

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


if __name__ == "__main__":
    app()

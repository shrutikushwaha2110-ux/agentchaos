"""A pass-through MCP proxy, with optional fault injection.

Usage (run from the project root, so the agentchaos package is importable):
    python -m agentchaos.proxy -- python test_server/server.py
    python -m agentchaos.proxy --error-rate 0.3 --seed 1 -- python test_server/server.py

Everything before "--" is proxy options (see faults.FaultConfig for what each
one does); everything after "--" is the upstream command to launch. The proxy
starts that upstream server, then acts as an MCP server itself: the agent
talks to the proxy, and the proxy forwards to upstream - unless a fault
fires first, or corrupts the result afterwards.

IMPORTANT: stdout is the wire to the agent. Never print() here - log to stderr.
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from agentchaos.events import EventTarget, fault_listener
from agentchaos.faults import FaultConfig, FaultInjector

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="[%(name)s] %(message)s")
log = logging.getLogger("proxy")


def parse_args(argv: list[str]) -> tuple[FaultConfig, str, list[str], EventTarget | None]:
    """Split argv on '--' into (proxy options, upstream command, upstream args,
    event target). The event target is None unless --events-file is given."""
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        sys.exit(
            "usage: python -m agentchaos.proxy [--error-rate R] [--timeout-rate R] "
            "[--timeout-seconds N] [--latency-ms N] [--malformed-rate R] "
            "[--injection-rate R] [--ratelimit-rate R] [--seed N] -- <upstream command> [args...]"
        )
    sep = argv.index("--")
    proxy_argv, upstream_argv = argv[:sep], argv[sep + 1:]

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--error-rate", type=float, default=0.0)
    parser.add_argument("--timeout-rate", type=float, default=0.0)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--latency-ms", type=int, default=0)
    parser.add_argument("--malformed-rate", type=float, default=0.0)
    parser.add_argument("--injection-rate", type=float, default=0.0)
    parser.add_argument("--ratelimit-rate", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=0)
    # Where to write fault events (see events.py). Optional: without it the
    # proxy only logs faults to stderr, as before.
    parser.add_argument("--events-file", type=str, default=None)
    parser.add_argument("--task-id", type=str, default="")
    parser.add_argument("--mode", type=str, default="")
    opts = parser.parse_args(proxy_argv)
    config = FaultConfig(
        error_rate=opts.error_rate,
        timeout_rate=opts.timeout_rate,
        timeout_seconds=opts.timeout_seconds,
        latency_ms=opts.latency_ms,
        malformed_rate=opts.malformed_rate,
        injection_rate=opts.injection_rate,
        ratelimit_rate=opts.ratelimit_rate,
        seed=opts.seed,
    )

    command, *args = upstream_argv
    # "python" on PATH may not be the venv's Python; sys.executable always is.
    if command in ("python", "python3"):
        command = sys.executable
    events = None
    if opts.events_file:
        events = EventTarget(path=Path(opts.events_file), task_id=opts.task_id, mode=opts.mode)
    return config, command, args, events


async def run_proxy(config: FaultConfig, command: str, args: list[str],
                    events: EventTarget | None = None) -> None:
    """Start the proxy: a client to `command`/`args` (the real server) and,
    at the same time, a server for the agent. Shared by both front ends -
    `python -m agentchaos.proxy ...` below, and the `agentchaos proxy`
    console script in cli.py. With `events`, each injected fault is also
    written to that run's event log.
    """
    injector = FaultInjector(config, on_fault=fault_listener(events, config.seed) if events else None)
    upstream_params = StdioServerParameters(command=command, args=args)

    # Step 1: act as a CLIENT of the real server.
    async with stdio_client(upstream_params) as (up_read, up_write):
        async with ClientSession(up_read, up_write) as upstream:
            await upstream.initialize()
            log.info("connected to upstream: %s %s", command, " ".join(args))
            log.info("fault config: %s", config)

            # Step 2: act as a SERVER for the agent, with handlers that forward.
            server = Server("agentchaos-proxy")

            @server.list_tools()
            async def list_tools() -> list[types.Tool]:
                tools: list[types.Tool] = []
                cursor = None
                while True:  # upstream may return tools in pages
                    page = await upstream.list_tools(cursor=cursor)
                    tools.extend(page.tools)
                    cursor = page.nextCursor
                    if cursor is None:
                        return tools

            # validate_input=False: the upstream validates arguments itself, so
            # the proxy forwards them unchanged and passes back whatever it says.
            @server.call_tool(validate_input=False)
            async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
                fault_result, call_index = await injector.before_call(name)
                if fault_result is not None:
                    return fault_result
                log.info("forwarding call: %s %s", name, arguments)
                result = await upstream.call_tool(name, arguments)
                # Returning the whole CallToolResult keeps isError and all content;
                # after_call() may still swap it for a corrupted "success".
                return injector.after_call(name, call_index, result)

            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())


async def main() -> None:
    config, command, args, events = parse_args(sys.argv[1:])
    await run_proxy(config, command, args, events)


if __name__ == "__main__":
    asyncio.run(main())

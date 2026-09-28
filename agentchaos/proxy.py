"""A pass-through MCP proxy, with optional fault injection.

Usage:
    python agentchaos/proxy.py -- python test_server/server.py
    python agentchaos/proxy.py --error-rate 0.3 --seed 1 -- python test_server/server.py

Everything before "--" is proxy options (see faults.FaultConfig for what each
one does); everything after "--" is the upstream command to launch. The proxy
starts that upstream server, then acts as an MCP server itself: the agent
talks to the proxy, and the proxy forwards to upstream - unless a fault
fires first.

IMPORTANT: stdout is the wire to the agent. Never print() here - log to stderr.
"""
import argparse
import asyncio
import logging
import sys

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from faults import FaultConfig, FaultInjector

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="[%(name)s] %(message)s")
log = logging.getLogger("proxy")


def parse_args(argv: list[str]) -> tuple[FaultConfig, str, list[str]]:
    """Split argv on '--' into (proxy options, upstream command, upstream args)."""
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        sys.exit(
            "usage: python agentchaos/proxy.py [--error-rate R] [--timeout-rate R] "
            "[--latency-ms N] [--seed N] -- <upstream command> [args...]"
        )
    sep = argv.index("--")
    proxy_argv, upstream_argv = argv[:sep], argv[sep + 1:]

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--error-rate", type=float, default=0.0)
    parser.add_argument("--timeout-rate", type=float, default=0.0)
    parser.add_argument("--latency-ms", type=int, default=0)
    parser.add_argument("--seed", type=int, default=0)
    opts = parser.parse_args(proxy_argv)
    config = FaultConfig(
        error_rate=opts.error_rate,
        timeout_rate=opts.timeout_rate,
        latency_ms=opts.latency_ms,
        seed=opts.seed,
    )

    command, *args = upstream_argv
    # "python" on PATH may not be the venv's Python; sys.executable always is.
    if command in ("python", "python3"):
        command = sys.executable
    return config, command, args


async def main() -> None:
    config, command, args = parse_args(sys.argv[1:])
    injector = FaultInjector(config)
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
                fault_result = await injector.maybe_inject(name)
                if fault_result is not None:
                    return fault_result
                log.info("forwarding call: %s %s", name, arguments)
                # Returning the whole CallToolResult keeps isError and all content.
                return await upstream.call_tool(name, arguments)

            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

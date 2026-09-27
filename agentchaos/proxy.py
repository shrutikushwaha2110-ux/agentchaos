"""A pass-through MCP proxy.

Usage:
    python agentchaos/proxy.py -- python test_server/server.py

The proxy starts the upstream server (everything after "--"), then acts as an
MCP server itself. The agent talks to the proxy; the proxy forwards to upstream.

IMPORTANT: stdout is the wire to the agent. Never print() here - log to stderr.
"""
import asyncio
import logging
import sys

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="[proxy] %(message)s")
log = logging.getLogger("agentchaos.proxy")


def parse_upstream_command(argv: list[str]) -> tuple[str, list[str]]:
    """Return (command, args) for everything after the '--' separator."""
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        sys.exit("usage: python agentchaos/proxy.py -- <upstream command> [args...]")
    command, *args = argv[argv.index("--") + 1:]
    # "python" on PATH may not be the venv's Python; sys.executable always is.
    if command in ("python", "python3"):
        command = sys.executable
    return command, args


async def main() -> None:
    command, args = parse_upstream_command(sys.argv[1:])
    upstream_params = StdioServerParameters(command=command, args=args)

    # Step 1: act as a CLIENT of the real server.
    async with stdio_client(upstream_params) as (up_read, up_write):
        async with ClientSession(up_read, up_write) as upstream:
            await upstream.initialize()
            log.info("connected to upstream: %s %s", command, " ".join(args))

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
                log.info("forwarding call: %s %s", name, arguments)
                # Returning the whole CallToolResult keeps isError and all content.
                return await upstream.call_tool(name, arguments)

            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

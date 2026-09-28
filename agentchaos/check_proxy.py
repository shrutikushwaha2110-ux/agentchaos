"""Check that the proxy is transparent: every call gives the same result
directly and through the proxy.

Run from the project root:  python -m agentchaos.check_proxy
"""
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).parent.parent
SERVER = str(ROOT / "test_server" / "server.py")

CALLS = [
    ("get_weather", {"city": "Delhi"}),
    ("get_weather", {"city": "Atlantis"}),  # isError result - must survive the proxy
    ("search_notes", {"query": "meeting"}),
    ("send_email", {"to": "a@example.com", "subject": "Hi", "body": "Testing."}),
]


@asynccontextmanager
async def connect(args: list[str], cwd: str | None = None):
    # cwd matters for the proxy: it's launched as `python -m agentchaos.proxy`,
    # and that only resolves if the process starts in the project root.
    params = StdioServerParameters(command=sys.executable, args=args, cwd=cwd)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def collect(session: ClientSession):
    """Return (tool list, [result for each call]) as plain dicts, easy to compare."""
    tools = [t.model_dump() for t in (await session.list_tools()).tools]
    results = [(await session.call_tool(name, args)).model_dump() for name, args in CALLS]
    return tools, results


async def main() -> int:
    async with connect([SERVER]) as direct:
        direct_tools, direct_results = await collect(direct)
    proxy_args = ["-m", "agentchaos.proxy", "--", sys.executable, SERVER]
    async with connect(proxy_args, cwd=str(ROOT)) as proxied:
        proxy_tools, proxy_results = await collect(proxied)

    failures = 0
    ok = direct_tools == proxy_tools
    print(f"[{'PASS' if ok else 'FAIL'}] tool list ({len(direct_tools)} tools)")
    failures += not ok
    for (name, args), d, p in zip(CALLS, direct_results, proxy_results):
        ok = d == p
        print(f"[{'PASS' if ok else 'FAIL'}] {name}({args}) isError={d['isError']}")
        if not ok:
            print("   direct:", d, "\n   proxied:", p)
        failures += not ok
    print("\nProxy is transparent." if not failures else f"\n{failures} check(s) failed.")
    return failures


sys.exit(asyncio.run(main()))

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).parent / "server.py"


async def main():
    # sys.executable = the Python running this script, so the venv is used automatically.
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            calls = [
                ("get_weather", {"city": "Delhi"}),
                ("get_weather", {"city": "Atlantis"}),
                ("search_notes", {"query": "meeting"}),
                ("send_email", {"to": "a@example.com", "subject": "Hi", "body": "Testing."}),
            ]
            for name, args in calls:
                result = await session.call_tool(name, args)
                status = "ERROR" if result.isError else "ok"
                print(f"{name}({args}) [{status}]\n  {result.content[0].text}\n")


asyncio.run(main())

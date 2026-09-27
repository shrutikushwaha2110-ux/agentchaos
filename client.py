import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    server = StdioServerParameters(command="python", args=["server.py"])
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("Tools:", [t.name for t in tools.tools])
            result = await session.call_tool("add", {"a": 2, "b": 3})
            print("add(2, 3) =", result.content[0].text)
            result = await session.call_tool("greet", {"name": "Saloni"})
            print(result.content[0].text)
            result = await session.call_tool("word_count", {"text": "chaos makes agents stronger"})
            print("word_count =", result.content[0].text)

asyncio.run(main())

"""Interoperability check with the official MCP Python client, when installed."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as temp:
        parameters = StdioServerParameters(command=sys.executable, args=[str(root / "server/app.py"), "mcp"], env={**os.environ, "COURSE_BRIDGE_DATA": temp})
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                assert len(listed.tools) == 15
                status = await session.call_tool("bridge_status", {})
                assert status.isError is False
                assert '"saved_materials": 0' in status.content[0].text
                await session.send_ping()
                print(f"Official MCP client: initialize/tools/list/bridge_status/ping OK ({initialized.protocolVersion}, {len(listed.tools)} tools)")


asyncio.run(main())

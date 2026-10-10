#!/usr/bin/env python3
"""End-to-end Ghidra MCP check from a worker-image container (run on the droplet, as root):

  docker run --rm --user 2000:2000 --cap-drop ALL --read-only --security-opt no-new-privileges --dns 127.0.0.1 \
    --tmpfs /tmp --add-host ghidra.host:172.17.0.1 -v /srv/decomp/shared/mcp:/opt/mcp:ro \
    -v /opt/bw1-sandbox/droplet/tests/ghidra_mcp_e2e.py:/e2e.py:ro --entrypoint python3 bw1-worker /e2e.py

A stdio MCP client (the image's mcp 2.x) starts the bridge with the command and env from /opt/mcp/mcp.json, as a
worker's Claude does, and calls read-only tools. Optional arguments limit it to those tool names.
"""
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CALLS = [
    ("search_functions_by_name", {"query": "TownStats", "limit": 5}),
    ("get_function_by_address", {"address": "00401350"}),
    ("decompile_function_by_address", {"address": "00401350"}),
    ("disassemble_function", {"address": "00401350"}),
    ("get_xrefs_to", {"address": "00401350", "limit": 5}),
    ("list_strings", {"filter": "Abode", "limit": 3}),
]


async def main():
    params = StdioServerParameters(command="python3", args=["/opt/mcp/bridge_mcp_ghidra.py", "--ghidra-server",
                                                            "http://ghidra.host:8080/"],
                                    env=json.load(open("/opt/mcp/mcp.json"))["mcpServers"]["ghidra"].get("env"))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            print("TOOLS:", len(tools), " ".join(tools))
            only = sys.argv[1:] or [c[0] for c in CALLS]
            for name, args in CALLS:
                if name not in only:
                    continue
                res = await s.call_tool(name, args)
                text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
                print(f"=== {name} {args}\n{text[:1500]}")


asyncio.run(main())

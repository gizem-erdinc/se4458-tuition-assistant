"""Demonstrates three distinct retrieval queries against the REAL MCP server,
through the MCP protocol (client -> stdio -> server), as required.

    python -m mcp_server.demo_queries
"""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
QUERIES = [
    "When is the fall term payment deadline?",
    "Who is eligible for an installment arrangement?",
    "How much refund do I get if I withdraw in the first two weeks?",
]


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"], cwd=str(ROOT))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("MCP tools:", [t.name for t in (await s.list_tools()).tools])
            for q in QUERIES:
                res = await s.call_tool("search_tuition_policies", {"query": q, "top_k": 2})
                data = json.loads(res.content[0].text)
                print(f"\nQ: {q}")
                print(f"   document={data['document']} ({data['document_id']}), matches={data['match_count']}")
                for hit in data["matches"]:
                    excerpt = " ".join(hit["text"].split())
                    print(f"  [{hit['score']:>6}] {hit['reference']}\n           {excerpt[:150]}…")


if __name__ == "__main__":
    asyncio.run(main())

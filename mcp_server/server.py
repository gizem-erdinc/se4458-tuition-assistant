"""MCP server exposing the indexed tuition policy document.

Tool: ``search_tuition_policies(query, top_k=3)``

Runs over stdio by default (the agent spawns it as a subprocess). It can also
be started as a standalone Streamable-HTTP server:

    python -m mcp_server.server --transport streamable-http --port 8765

The index is loaded once at start-up (built only if missing/outdated).
"""
from __future__ import annotations

import argparse
import logging
import sys

from mcp.server.mcpserver import MCPServer

from mcp_server.indexer import DEFAULT_DOC, DEFAULT_INDEX, PolicyIndex

# stdio transport uses stdout for protocol messages -> log to stderr only.
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s [mcp-policy] %(message)s")
log = logging.getLogger("mcp-policy")

INDEX, REBUILT = PolicyIndex.load_or_build(DEFAULT_DOC, DEFAULT_INDEX)
log.info("policy index %s: %s, %d chunks, doc sha256=%s…", "rebuilt" if REBUILT else "loaded",
         INDEX.document_id, len(INDEX.chunks), INDEX.doc_sha256[:12])

mcp = MCPServer(
    name="tuition-policy-server",
    instructions=(
        "Search the university tuition policy document (deadlines, installments, "
        "late fees, scholarships, refunds, banking instructions). Returns passages "
        "with section references. It does NOT contain any student's balance."
    ),
)


@mcp.tool()
def search_tuition_policies(query: str, top_k: int = 3) -> dict:
    """Search the official University Tuition Policies document.

    Use for questions about rules: payment deadlines, payment posting, installment
    eligibility/size/schedule, partial payments, late fees, scholarships, refunds and
    withdrawals, banking/payment instructions. Returns the most relevant passages
    with section ID, heading, document ID and text so the answer can cite them.
    This tool has NO student-specific data (balances live in the Tuition API).

    Args:
        query: Natural-language search query, e.g. "installment eligibility".
        top_k: Number of excerpts to return (1-5).
    """
    top_k = max(1, min(int(top_k), 5))
    results = INDEX.search(query, top_k=top_k)
    log.info("search %r -> %s", query, [r["section"] for r in results])
    return {
        "document": INDEX.source,
        "document_id": INDEX.document_id,
        "query": query,
        "match_count": len(results),
        "matches": results,
        "note": None if results else "No matching policy section found.",
    }

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()

"""
MCP server for retrieval.

Exposes the retrieval capabilities as standard MCP tools so any MCP-compatible
client (here, the agentic orchestrator) can discover and invoke them without
bespoke per-tool wiring. This is the integration layer above raw function
calling: build the tool server once, any client can consume it.

Tools exposed:
  - search_news(query, top_k, recency_days, sources)  -> list[Article]
  - fetch_article(source_id)                          -> Article | null

The server talks to the existing retrieval FastAPI service over HTTP, so the
vector store stays owned by one service. Run with:

    python -m app.mcp_server            # stdio transport (for local clients)
    MCP_HTTP=1 python -m app.mcp_server  # streamable HTTP transport
"""
from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

RETRIEVAL_URL = os.getenv("RETRIEVAL_URL", "http://retrieval:8000")

mcp = FastMCP("news-retrieval")


@mcp.tool()
def search_news(
    query: str,
    top_k: int = 8,
    recency_days: int | None = 14,
    sources: list[str] | None = None,
) -> list[dict]:
    """Search recent news articles in the vector store.

    Use a focused query. If results look thin or off-topic, call again with a
    refined query or a wider recency_days window.
    """
    with httpx.Client(timeout=60) as hc:
        r = hc.post(
            f"{RETRIEVAL_URL}/search",
            json={
                "query": query,
                "top_k": top_k,
                "recency_days": recency_days,
                "sources": sources,
            },
        )
        r.raise_for_status()
        return r.json()["articles"]


@mcp.tool()
def fetch_article(source_id: str) -> dict | None:
    """Fetch the full stored chunk for a specific source_id (for follow-up)."""
    with httpx.Client(timeout=60) as hc:
        r = hc.post(f"{RETRIEVAL_URL}/by_id", json={"source_id": source_id})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


if __name__ == "__main__":
    if os.getenv("MCP_HTTP"):
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")

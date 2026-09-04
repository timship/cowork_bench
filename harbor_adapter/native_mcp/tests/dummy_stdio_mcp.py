#!/usr/bin/env python3
"""Minimal stdio MCP server for gateway tests."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("dummy")


@mcp.tool()
def ping() -> str:
    return "pong"


if __name__ == "__main__":
    mcp.run(transport="stdio")

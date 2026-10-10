"""Tool layer: read-only queries and their MCP tool wrappers.

No transport or auth code lives here. `queries` is plain Python over SQLite;
`mcp_tools` exposes those queries as MCP tools on any FastMCP server.
"""

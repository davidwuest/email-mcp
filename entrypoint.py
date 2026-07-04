import os
from simple_email_mcp import mcp

transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
host = os.environ.get("MCP_HOST", "0.0.0.0")
port = int(os.environ.get("MCP_PORT", "8000"))

# host/port are configured on settings; FastMCP.run() no longer accepts them.
mcp.settings.host = host
mcp.settings.port = port

mcp.run(transport=transport)

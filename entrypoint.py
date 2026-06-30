import os
from simple_email_mcp import mcp

transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
host = os.environ.get("MCP_HOST", "0.0.0.0")
port = int(os.environ.get("MCP_PORT", "8000"))

mcp.run(transport=transport, host=host, port=port)

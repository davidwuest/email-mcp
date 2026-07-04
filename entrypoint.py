import os
from simple_email_mcp import mcp

transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
host = os.environ.get("MCP_HOST", "0.0.0.0")
port = int(os.environ.get("MCP_PORT", "8000"))

# host/port are configured on settings; FastMCP.run() no longer accepts them.
mcp.settings.host = host
mcp.settings.port = port

# When served behind a reverse proxy under a public hostname, the streamable-http
# transport's DNS-rebinding protection rejects the external Host header ("Invalid
# Host header", 421). Set MCP_ALLOWED_HOSTS (comma-separated, e.g.
# "mcp.example.com,mcp.example.com:*") to allow it; both http and https origins
# are permitted for each entry.
allowed_hosts_env = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
if allowed_hosts_env:
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = [h.strip() for h in allowed_hosts_env.split(",") if h.strip()]
    origins = []
    for h in hosts:
        origins.append(f"https://{h}")
        origins.append(f"http://{h}")
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )

mcp.run(transport=transport)

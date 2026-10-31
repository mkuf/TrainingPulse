"""Entry point: ``python -m mcp_server``."""

from mcp.server.transport_security import TransportSecuritySettings

from config import config
from mcp_server.server import mcp


def _allowed_origins(allowed_hosts: list[str]) -> list[str]:
    origins: list[str] = []
    for host in allowed_hosts:
        if host.startswith(("http://", "https://")):
            origins.append(host)
            continue
        origins.append(f"http://{host}")
        origins.append(f"https://{host}")
    return origins


mcp.run(
    transport="streamable-http",
    host=config.mcp.host,
    port=config.mcp.port,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=config.mcp.allowed_hosts,
        allowed_origins=_allowed_origins(config.mcp.allowed_hosts),
    ),
)

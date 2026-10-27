"""Entry point: ``python -m mcp_server``."""

from config import config
from mcp_server.server import mcp

mcp.settings.host = config.mcp.host
mcp.settings.port = config.mcp.port
mcp.run(transport="streamable-http")

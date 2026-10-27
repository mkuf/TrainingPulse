"""TrainingPulse MCP server.

Exposes read-only, training-aware tools over MCP for local/network clients.
Run it with ``python -m mcp_server``.

Deliberately empty of imports: the modules load ``config`` when imported, and
the tests need to choose that config first.
"""

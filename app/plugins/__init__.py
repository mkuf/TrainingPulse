"""In-process TrainingPulse plugin registry."""

from plugins.registry import dispose_plugins, load_plugins, register_plugin_mcp_tools, setup_plugins

__all__ = [
    "load_plugins",
    "setup_plugins",
    "register_plugin_mcp_tools",
    "dispose_plugins",
]

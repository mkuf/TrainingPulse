"""Activity sources: one adapter per provider, one engine for all of them.

Deliberately empty of imports. The provider clients import
:mod:`sources.base` for the shared rate-limit exception, and the adapters
import those clients back, so re-exporting the registry here would create an
import cycle. Import the submodules you need directly.
"""

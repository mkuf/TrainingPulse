"""Sync engines: one for activities, one for every other data kind.

Providers only list and map; everything else about a sync run (windows,
upserts, enrichment, and deciding which provider's rows are visible) lives
here once for every provider.
"""

#!/usr/bin/env python3
"""Push dashboards from grafana/dashboards/ to a Grafana instance via HTTP API.

Env: GRAFANA_TLS_SKIP_VERIFY=1 (or true/yes/on) disables TLS certificate verification
for https:// URLs (e.g. self-signed Grafana). Use only on trusted networks.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def _https_open_kwargs(url: str) -> dict:
    if not url.lower().startswith("https://"):
        return {}
    if not _truthy(os.environ.get("GRAFANA_TLS_SKIP_VERIFY")):
        return {}
    return {"context": ssl._create_unverified_context()}


def main() -> int:
    token = os.environ.get("GRAFANA_TOKEN")
    if not token:
        print("GRAFANA_TOKEN is required", file=sys.stderr)
        return 1

    base = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
    try:
        folder_id = int(os.environ.get("GRAFANA_FOLDER_ID", "0"))
    except ValueError:
        print("GRAFANA_FOLDER_ID must be an integer", file=sys.stderr)
        return 1

    dashboards_dir = Path(__file__).resolve().parent / "dashboards"
    if not dashboards_dir.is_dir():
        print(f"Dashboards directory not found: {dashboards_dir}", file=sys.stderr)
        return 1

    paths = sorted(dashboards_dir.glob("*.json"))
    if not paths:
        print(f"No JSON dashboards in {dashboards_dir}", file=sys.stderr)
        return 1

    url = f"{base}/api/dashboards/db"
    failed = False

    for path in paths:
        try:
            dashboard = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"{path.name}: read error: {e}", file=sys.stderr)
            failed = True
            continue

        dashboard["id"] = None
        body = json.dumps(
            {"dashboard": dashboard, "overwrite": True, "folderId": folder_id}
        ).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, **_https_open_kwargs(url)) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            snippet = err_body[:500] + ("…" if len(err_body) > 500 else "")
            print(f"{path.name}: HTTP {e.code} {snippet}", file=sys.stderr)
            failed = True
            continue
        except urllib.error.URLError as e:
            print(f"{path.name}: request failed: {e.reason}", file=sys.stderr)
            failed = True
            continue

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            print(f"{path.name}: ok (non-JSON response)")
            continue

        status = payload.get("status")
        uid = payload.get("uid", "")
        if status == "success":
            print(f"{path.name}: ok uid={uid}")
        else:
            print(f"{path.name}: unexpected response: {raw[:500]}", file=sys.stderr)
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

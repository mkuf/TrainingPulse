"""Run inside a container of the runtime image: wait for a service to answer.

``check.py app`` polls the status page and the sync API on port 8000;
``check.py mcp`` sends an MCP ``initialize`` and ``tools/list`` to port 8001.
Exits non-zero if the service is not up within a minute.
"""

import json
import sys
import time
import urllib.error
import urllib.request

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def request(url: str, body: dict | None = None) -> bytes:
    data = json.dumps(body).encode() if body is not None else None
    headers = MCP_HEADERS if body is not None else {}
    with urllib.request.urlopen(
        urllib.request.Request(url, data=data, headers=headers), timeout=10
    ) as response:
        return response.read()


def check_app() -> None:
    page = request("http://localhost:8000/").decode()
    assert "TrainingPulse" in page, page[:500]
    status = json.loads(request("http://localhost:8000/sync/status"))
    assert "is_running" in status, status
    request("http://localhost:8000/static/css/app.css")


def check_mcp() -> None:
    url = "http://localhost:8001/mcp"
    init = json.loads(
        request(
            url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke-test", "version": "1"},
                },
            },
        )
    )
    assert init["result"]["serverInfo"]["name"] == "TrainingPulse MCP", init
    tools = json.loads(
        request(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    )
    assert any(t["name"] == "list_activities" for t in tools["result"]["tools"]), tools


def main() -> None:
    check = {"app": check_app, "mcp": check_mcp}[sys.argv[1]]
    deadline = time.monotonic() + 60
    while True:
        try:
            check()
        except (urllib.error.URLError, ConnectionError) as exc:
            if time.monotonic() > deadline:
                raise SystemExit(f"{sys.argv[1]} did not come up: {exc}")
            time.sleep(1)
            continue
        print(f"{sys.argv[1]}: ok")
        return


if __name__ == "__main__":
    main()

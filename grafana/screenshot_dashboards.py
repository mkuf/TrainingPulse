"""Capture full-page screenshots of the bundled Grafana dashboards.

Intended to run inside `mcr.microsoft.com/playwright/python:v1.49.1-jammy`,
joined to the `strava-sync_default` docker network so it can reach Grafana
at `http://grafana:3000`. Driven by [grafana/take_screenshots.sh](take_screenshots.sh).

Environment variables (with defaults):
    GRAFANA_URL        http://grafana:3000
    OUT_DIR            /out
    WIDTH              1920
    HEIGHT             1080            # initial viewport; full_page=True grows it
    WAIT_MS            15000           # tail wait after networkidle for paint to settle
    TIMERANGE          from=now-30d&to=now (fitness, account_overview, addon dashboards)
    ACTIVITY_ID        (required) numeric id used for the activity-detail dashboard
    ACTIVITY_FROM_MS   (required) epoch-ms start for the activity-detail time range
    ACTIVITY_TO_MS     (required) epoch-ms end for the activity-detail time range
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass

from playwright.async_api import Page, async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("screenshot")


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        log.error("missing required env var: %s", name)
        sys.exit(2)
    return value or ""


@dataclass
class Dashboard:
    name: str
    path: str


def build_targets() -> list[Dashboard]:
    grafana = env("GRAFANA_URL", "http://grafana:3000").rstrip("/")
    timerange = env("TIMERANGE", "from=now-30d&to=now")
    activity_id = env("ACTIVITY_ID", required=True)
    activity_from = env("ACTIVITY_FROM_MS", required=True)
    activity_to = env("ACTIVITY_TO_MS", required=True)

    return [
        Dashboard(
            name="fitness",
            path=f"{grafana}/d/trainingpulse-fitness/trainingpulse-fitness-and-freshness?orgId=1&{timerange}&kiosk",
        ),
        Dashboard(
            name="account_overview",
            path=f"{grafana}/d/trainingpulse-account/trainingpulse-account-overview?orgId=1&{timerange}&kiosk",
        ),
        Dashboard(
            name="activity_detail",
            path=(
                f"{grafana}/d/trainingpulse-activity-detail/trainingpulse-activity-detail"
                f"?orgId=1&from={activity_from}&to={activity_to}"
                f"&var-activity_id={activity_id}&kiosk"
            ),
        ),
        Dashboard(
            name="nutrition_training_weight",
            path=(
                f"{grafana}/d/trainingpulse-nutrition-training-weight/"
                f"trainingpulse-nutrition-training-and-weight"
                f"?orgId=1&{timerange}&kiosk"
                f"&var-DS_TRAINING=trainingpulse-pg"
                f"&var-DS_NUTRITION=fddb-pg"
                f"&var-DS_WEIGHT=withings-pg"
            ),
        ),
    ]


async def trigger_lazy_load(page: Page, step: int = 800, settle_ms: int = 400) -> None:
    """Scroll from top to bottom in steps so Grafana renders lazy panels, then back to top."""
    scroll_height = await page.evaluate("() => document.body.scrollHeight")
    for y in range(0, scroll_height + step, step):
        await page.evaluate(f"window.scrollTo(0, {y})")
        await page.wait_for_timeout(settle_ms)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(settle_ms)


async def capture(page: Page, dash: Dashboard, out_dir: str, tail_wait_ms: int) -> str:
    log.info("loading %s", dash.path)
    await page.goto(dash.path, wait_until="networkidle", timeout=60_000)
    await trigger_lazy_load(page)
    try:
        await page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception as exc:
        log.warning("networkidle wait timed out for %s: %s", dash.name, exc)
    await page.wait_for_timeout(tail_wait_ms)
    out_path = os.path.join(out_dir, f"{dash.name}.png")
    await page.screenshot(path=out_path, full_page=True)
    size = os.path.getsize(out_path)
    log.info("wrote %s (%d bytes)", out_path, size)
    return out_path


async def main() -> int:
    out_dir = env("OUT_DIR", "/out")
    width = int(env("WIDTH", "1920"))
    height = int(env("HEIGHT", "1080"))
    wait_ms = int(env("WAIT_MS", "15000"))

    os.makedirs(out_dir, exist_ok=True)
    targets = build_targets()
    failed = False

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        try:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
                locale="en-US",
                timezone_id="UTC",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            for dash in targets:
                try:
                    await capture(page, dash, out_dir, wait_ms // 5)
                except Exception as exc:
                    log.exception("failed to capture %s: %s", dash.name, exc)
                    failed = True
            await context.close()
        finally:
            await browser.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

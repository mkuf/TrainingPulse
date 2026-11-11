"""Form in percent of fitness, its bands, and the Form panel that shows them."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from metrics import FORM_STATUSES, form_pct, form_status

FITNESS_DASHBOARD = (
    Path(__file__).resolve().parents[2] / "grafana" / "dashboards" / "fitness.json"
)
#: The limits between the bands, lowest first.
LIMITS = sorted(lowest for lowest, _ in FORM_STATUSES if lowest != float("-inf"))


def test_form_pct_is_tsb_in_percent_of_fitness():
    assert form_pct(-20.0, 80.0) == -25.0
    assert form_pct(10.0, 50.0) == 20.0


@pytest.mark.parametrize("tsb, ctl", [(None, 50.0), (5.0, None), (5.0, 0.0), (0.0, -1.0)])
def test_form_pct_is_none_without_fitness(tsb, ctl):
    assert form_pct(tsb, ctl) is None


@pytest.mark.parametrize(
    "pct, status",
    [
        (60.0, "transition"),
        (25.0, "transition"),
        (24.9, "fresh"),
        (5.0, "fresh"),
        (0.0, "neutral"),
        (-10.0, "neutral"),
        (-10.1, "optimal_training"),
        (-30.0, "optimal_training"),
        (-30.1, "high_risk"),
        (-400.0, "high_risk"),
        (None, None),
    ],
)
def test_form_status_bands(pct, status):
    assert form_status(pct) == status


def _form_panel() -> dict:
    dashboard = json.loads(FITNESS_DASHBOARD.read_text())
    return next(p for p in dashboard["panels"] if p["title"].startswith("Form"))


def test_form_panel_colors_use_the_same_limits():
    steps = _form_panel()["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert [step["value"] for step in steps[1:]] == LIMITS


def test_form_panel_state_uses_the_same_limits():
    state_sql = next(
        t["rawSql"] for t in _form_panel()["targets"] if '"State"' in t["rawSql"]
    )
    limits = [float(n) for n in re.findall(r"< (-?\d+(?:\.\d+)?) THEN", state_sql)]
    assert sorted(limits) == LIMITS

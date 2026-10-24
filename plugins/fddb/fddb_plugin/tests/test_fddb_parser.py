"""Tests for FDDB HTML parsing."""

from datetime import date
from pathlib import Path

import pytest

from fddb_plugin.client import (
    FddbAuthenticationError,
    FddbNoDataError,
    FddbParseError,
    parse_diary_html,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_parse_diary_with_data():
    html = (FIXTURES / "valid-response.html").read_text(encoding="utf-8")
    data = parse_diary_html(html, date(2024, 8, 27))
    assert data.kcal == 2565
    assert data.fat_g == 110.4
    assert data.carbs_g == 246.2
    assert data.protein_g == 126.4
    assert data.sugar_g == 51
    assert data.fiber_g == 18.3


def test_parse_diary_unauthenticated():
    html = (FIXTURES / "unauthenticated.html").read_text(encoding="utf-8")
    with pytest.raises(FddbAuthenticationError):
        parse_diary_html(html, date(2024, 8, 28))


def test_parse_diary_no_data():
    html = (FIXTURES / "no-data-available.html").read_text(encoding="utf-8")
    with pytest.raises(FddbNoDataError):
        parse_diary_html(html, date(2024, 8, 28))


def test_parse_diary_redesign_layout():
    """Oct 2026 redesign: combined "kJ (kcal)" column, meal subtotals as rows."""
    html = (FIXTURES / "valid-response-redesign.html").read_text(encoding="utf-8")
    data = parse_diary_html(html, date(2026, 10, 22))
    assert data.kcal == 1391
    assert data.fat_g == 97.5
    assert data.carbs_g == 185.25
    assert data.protein_g == 282.75
    assert data.sugar_g == 12.5
    assert data.fiber_g == 23.75


def test_parse_diary_redesign_empty_day():
    html = (
        '<main class="fddb-myday"><table class="myday-table-std"><tbody>'
        "<tr><th>Product</th></tr>"
        '<tr><td class="myday-table-std-td-footer"></td>'
        '<td class="myday-table-std-td-footer">0 kJ <span>(0 kcal)</span></td>'
        '<td class="myday-table-std-td-footer">0 g</td>'
        '<td class="myday-table-std-td-footer">0 g</td>'
        '<td class="myday-table-std-td-footer">0 g</td></tr>'
        "</tbody></table></main>"
    )
    with pytest.raises(FddbNoDataError):
        parse_diary_html(html, date(2026, 10, 22))


def test_parse_diary_unrecognized_totals_row():
    html = (
        '<table class="myday-table-std"><tbody>'
        "<tr><td>foo</td><td>bar</td></tr></tbody></table>"
    )
    with pytest.raises(FddbParseError):
        parse_diary_html(html, date(2026, 10, 22))

"""Tests for FDDB HTML parsing."""

from datetime import date
from pathlib import Path

import pytest

from fddb_plugin.client import (
    FddbAuthenticationError,
    FddbNoDataError,
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

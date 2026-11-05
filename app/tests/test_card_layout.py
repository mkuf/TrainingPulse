"""Tests for the row count behind the status page's account cards."""

from __future__ import annotations

import pytest
from card_layout import card_rows


@pytest.mark.parametrize(
    ("spans", "columns", "expected"),
    [
        ([], 3, 0),
        ([1, 1], 3, 1),
        # A tall card beside two small ones stacked.
        ([2, 1, 1], 3, 2),
        ([2, 2, 1, 1], 3, 2),
        # Two tall cards with the small ones underneath, no gap.
        ([2, 2, 1, 1], 2, 3),
        # The total would fit 3 rows, but these heights cannot.
        ([3, 2, 2, 2], 3, 4),
        ([3, 2, 1, 2, 1], 3, 3),
    ],
)
def test_card_rows_is_the_fewest_that_fit(spans, columns, expected):
    assert card_rows(spans, columns) == expected

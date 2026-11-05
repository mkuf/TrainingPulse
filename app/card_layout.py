"""How many grid rows the status page's account cards need."""

from __future__ import annotations


def card_rows(spans: list[int], columns: int) -> int:
    """Fewest rows that hold cards of these heights in this many columns.

    Mirrors how the page's grid places them: each card goes in the first column
    with room for it, in order.
    """
    rows = max(spans, default=0)
    while True:
        used = [0] * columns
        for span in spans:
            column = next((i for i in range(columns) if used[i] + span <= rows), None)
            if column is None:
                break
            used[column] += span
        else:
            return rows
        rows += 1

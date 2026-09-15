"""Shared pytest fixtures for the TrainingPulse core tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

MINIMAL_CONFIG = """
athlete:
  max_hr: 190
  rest_hr: 60
  ftp: 250
"""


@pytest.fixture
def write_config(tmp_path: Path):
    """Write a YAML config file and return its path."""

    def _write(body: str, name: str = "config.yaml") -> Path:
        path = tmp_path / name
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def minimal_config(write_config):
    """A config file with only the required athlete section."""
    return write_config(MINIMAL_CONFIG)

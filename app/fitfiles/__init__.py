"""Generic FIT file analysis and storage.

This package has no knowledge of any particular activity source (Hammerhead,
or any future FIT-capable provider). ``analyzer`` turns raw ``.fit`` bytes into
a summary of aggregate metrics plus canonical time-series streams; ``store``
persists the raw bytes so a file is only ever downloaded once per activity and
can be re-analyzed later without hitting the provider again.
"""

from fitfiles.analyzer import FitAnalysis, FitParseError, FitSummary, analyze_fit_file
from fitfiles.store import (
    MAX_RAW_DATA_BYTES,
    FitFileTooLargeError,
    get_fit_bytes,
    save_fit_bytes,
)

__all__ = [
    "FitAnalysis",
    "FitFileTooLargeError",
    "FitParseError",
    "FitSummary",
    "MAX_RAW_DATA_BYTES",
    "analyze_fit_file",
    "get_fit_bytes",
    "save_fit_bytes",
]

"""Application configuration.

Loaded once from ``config.yaml`` at import time. The schema, the ``TP_*``
environment override scheme, and the ``${VAR}`` interpolation rules all live in
:mod:`trainingpulse_common.config`.

Importing this module fails fast with a readable message (and a non-zero exit
code) when the configuration is missing or invalid, rather than letting a
half-configured process start.
"""

from trainingpulse_common.config import Config, load_config_or_exit

config: Config = load_config_or_exit()

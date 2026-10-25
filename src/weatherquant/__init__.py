"""WeatherQuant Next - reproducible quantitative research core for Polymarket weather markets.

Scope: read-only public data ingestion, probability estimation, and research/paper
evaluation. This package contains no code path that can transmit an order.
"""

# The version comes from the installed distribution metadata, never from a
# second hand-maintained constant here: the two drifted (pyproject 0.1.3 vs
# __version__ 0.1.0), and every audit surface that reports app_version -
# heartbeats, collector_runs, shadow runs - was quietly reporting the wrong
# one (WQN-4). Falls back only when imported from a bare source tree with no
# metadata, which no deployment does.
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

try:
    __version__ = _distribution_version("weatherquant-next")
except PackageNotFoundError:  # pragma: no cover - bare source tree
    __version__ = "0.0.0+source-tree"

"""Adapters that map another tool's results to the tocsin.eval/v1 report.

Standard library only: they run on the machines that run the jobs, from the
dependency-free base install.
"""

from __future__ import annotations

SCHEMA = "tocsin.eval/v1"
# The most scenarios the server accepts in one report.
MAX_SCENARIOS = 1000
MAX_NAME_LENGTH = 200


class AdapterError(ValueError):
    """The input can't be turned into a report; the message says why."""

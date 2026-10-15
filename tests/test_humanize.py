from __future__ import annotations

from datetime import timedelta

import pytest

from tocsin.humanize import duration


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (45, "45s"), (1860, "31m"), (630, "10m 30s"), (7500, "2h 5m"), (273600, "3d 4h")],
)
def test_duration(seconds: int, text: str) -> None:
    assert duration(seconds) == text
    assert duration(timedelta(seconds=seconds)) == text

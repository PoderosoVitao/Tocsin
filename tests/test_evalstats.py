from __future__ import annotations

import math
import time
from fractions import Fraction

import pytest

from tocsin.evalstats import (
    compare,
    p_value_lower,
    runs_needed,
    wilson_interval,
)

# Computed by Stochast's own wilson_interval (stochast 0.2.1), so tocsin
# reports a scenario's pass rate the same way Stochast does.
STOCHAST_WILSON = [
    (0, 1, 0.0, 0.7934506856227626),
    (1, 1, 0.20654931437723745, 1.0),
    (0, 10, 2.7755575615628914e-17, 0.27753279986288915),
    (10, 10, 0.7224672001371109, 1.0),
    (47, 50, 0.8378290831116182, 0.979385029651026),
    (3, 4, 0.300641842582402, 0.9544127391902995),
    (70, 100, 0.6041514536665333, 0.7810511470506722),
    (94, 100, 0.8752318455410406, 0.9722138760368118),
    (940, 1000, 0.9235289252086434, 0.953103527324068),
    (1, 2, 0.09453120573423074, 0.9054687942657693),
    (5, 5, 0.5655175352168254, 1.0),
    (0, 50, 0.0, 0.07134759913335868),
]


@pytest.mark.parametrize(("successes", "n", "low", "high"), STOCHAST_WILSON)
def test_wilson_matches_stochast(successes: int, n: int, low: float, high: float) -> None:
    got_low, got_high = wilson_interval(successes, n)
    assert got_low == pytest.approx(low, abs=1e-12)
    assert got_high == pytest.approx(high, abs=1e-12)


def test_wilson_stays_inside_zero_and_one_and_around_the_estimate() -> None:
    for n in (1, 5, 50, 500):
        for successes in range(0, n + 1, max(1, n // 10)):
            low, high = wilson_interval(successes, n)
            assert 0.0 <= low <= successes / n <= high <= 1.0


def test_wilson_narrows_as_samples_grow() -> None:
    widths = [
        wilson_interval(9 * n, 10 * n)[1] - wilson_interval(9 * n, 10 * n)[0] for n in (1, 10, 100)
    ]
    assert widths == sorted(widths, reverse=True)


def test_wilson_confidence_level_changes_the_width() -> None:
    narrow = wilson_interval(47, 50, confidence=0.80)
    wide = wilson_interval(47, 50, confidence=0.99)
    assert wide[0] < narrow[0] < narrow[1] < wide[1]


@pytest.mark.parametrize("args", [(0, 0), (5, 4), (-1, 4)])
def test_counts_are_validated(args: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="n must be positive|between 0 and n"):
        wilson_interval(*args)
    with pytest.raises(ValueError):
        p_value_lower(*args, 5, 10)
    with pytest.raises(ValueError):
        p_value_lower(5, 10, *args)


# Fisher's tea-tasting table (3 of 4 against 1 of 4) has a known one-sided
# p-value of 17/70.
def test_p_value_matches_the_textbook_example() -> None:
    assert p_value_lower(3, 4, 1, 4) == pytest.approx(17 / 70, abs=1e-12)


# An independent check: the same probability with exact integer arithmetic,
# over every small table, against the log-space implementation.
def exact_p_value_lower(base_s: int, base_n: int, cur_s: int, cur_n: int) -> Fraction:
    passes, total = base_s + cur_s, base_n + cur_n
    ways = sum(
        math.comb(cur_n, k) * math.comb(base_n, passes - k)
        for k in range(max(0, passes - base_n), cur_s + 1)
    )
    return Fraction(ways, math.comb(total, passes))


def test_p_value_agrees_with_exact_arithmetic_on_every_small_table() -> None:
    checked = 0
    for base_n in (1, 3, 8, 12):
        for cur_n in (1, 2, 7, 12):
            for base_s in range(base_n + 1):
                for cur_s in range(cur_n + 1):
                    expected = float(exact_p_value_lower(base_s, base_n, cur_s, cur_n))
                    assert p_value_lower(base_s, base_n, cur_s, cur_n) == pytest.approx(
                        expected, abs=1e-12
                    )
                    checked += 1
    assert checked > 500


def test_p_value_is_about_half_for_identical_rates_and_near_one_for_a_rise() -> None:
    assert 0.4 < p_value_lower(90, 100, 9, 10) < 0.75
    assert p_value_lower(70, 100, 90, 100) > 0.999


def test_p_value_agrees_with_the_normal_approximation_for_big_samples() -> None:
    # 94% vs 93% over 10,000 runs each: z is about 2.9, one-sided p about 0.002.
    assert 0.001 < p_value_lower(9400, 10000, 9300, 10000) < 0.004


def test_p_value_is_fast_for_huge_baselines() -> None:
    started = time.monotonic()
    p = p_value_lower(940_000, 1_000_000, 930_000, 1_000_000)
    assert time.monotonic() - started < 2
    assert 0 <= p < 1e-6


def test_a_clear_drop_is_a_regression() -> None:
    result = compare(940, 1000, 35, 50)
    assert result.verdict == "regression"
    assert (result.baseline_rate, result.current_rate) == (0.94, 0.7)
    assert result.drop == pytest.approx(0.24)
    assert result.p_value < 1e-5
    assert result.runs_needed is None


# Section 16: "one with too few runs reports not enough data". A drop from 94%
# to 4 of 5 is 14 points, but five runs can't show it isn't luck.
def test_a_drop_on_too_few_runs_needs_more_data_and_says_how_many() -> None:
    result = compare(94, 100, 4, 5)
    assert result.verdict == "needs_data"
    assert result.p_value > 0.05
    assert result.runs_needed == 18
    # Believing the estimate: 18 more runs at the same rate would settle it.
    assert p_value_lower(94, 100, 4 + round(18 * 0.8), 5 + 18) < 0.05


def test_the_same_drop_on_enough_runs_is_a_regression() -> None:
    assert compare(94, 100, 16, 20).verdict == "needs_data"
    assert compare(94, 100, 32, 40).verdict == "regression"


def test_no_drop_or_an_improvement_is_ok() -> None:
    assert compare(70, 100, 90, 100).verdict == "ok"
    assert compare(94, 100, 94, 100).verdict == "ok"
    assert compare(94, 100, 94, 100).runs_needed is None


# A real but tiny change (a point, with thousands of runs behind it) is
# statistically clear and still not worth waking anyone for.
def test_a_drop_smaller_than_the_minimum_is_never_a_regression() -> None:
    result = compare(9400, 10000, 9300, 10000)
    assert result.p_value < 0.01
    assert result.verdict == "ok"
    assert compare(9400, 10000, 9300, 10000, min_drop=0.005).verdict == "regression"


def test_alpha_decides_how_much_proof_is_needed() -> None:
    assert compare(94, 100, 7, 10).verdict == "regression"
    assert compare(94, 100, 7, 10, alpha=0.01).verdict == "needs_data"


def test_runs_needed_is_none_when_there_is_no_drop_or_it_never_settles() -> None:
    assert runs_needed(70, 100, 90, 100) is None
    assert runs_needed(94, 100, 94, 100) is None
    # A drop too small to ever prove within the search limit.
    assert runs_needed(100_000, 200_000, 99_999, 200_000) is None

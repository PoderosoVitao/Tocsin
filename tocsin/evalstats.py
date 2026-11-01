from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Literal

Interval = tuple[float, float]
Verdict = Literal["ok", "regression", "needs_data"]

# A drop smaller than this many percentage points (as a fraction) is never
# worth an alert, however many runs prove it is real.
DEFAULT_MIN_DROP = 0.05
DEFAULT_ALPHA = 0.05
# Stop searching for "how many more runs would settle it" past this.
MAX_RUNS_NEEDED = 100_000


def _check_counts(successes: int, n: int) -> None:
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be between 0 and n")


# A Wilson score interval for a binomial proportion, the same formula and
# default (95%) as Stochast's reports. Unlike the normal approximation it stays
# within [0, 1] and behaves for small samples and rates near 0 or 1, which is
# where evaluation pass rates live.
def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> Interval:
    _check_counts(successes, n)
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    phat = successes / n
    denominator = 1 + z**2 / n
    center = (phat + z**2 / (2 * n)) / denominator
    margin = z * ((phat * (1 - phat) / n + z**2 / (4 * n**2)) ** 0.5) / denominator
    # With no passes the lower bound is exactly 0, and with only passes the
    # upper bound is exactly 1; the formula leaves rounding noise (about 1e-17)
    # there, which would put the interval just off its own point estimate.
    low = 0.0 if successes == 0 else max(0.0, center - margin)
    high = 1.0 if successes == n else min(1.0, center + margin)
    return low, high


def _log_choose(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


# The one-sided p-value of Fisher's exact test that the current batch's pass
# rate is lower than the baseline's: given the total number of passes across
# both, the probability of the current batch having as few passes as it did, or
# fewer, if both came from the same underlying rate.
#
# Exact rather than a normal approximation because a nightly batch is often a
# few dozen runs, too few for an approximation to be trusted (the same reason
# the intervals are Wilson's). One-sided because only a drop matters here; a
# rise in the pass rate is never an alert. Computed in log space so that
# baselines of thousands of runs don't overflow.
def p_value_lower(
    baseline_successes: int, baseline_n: int, current_successes: int, current_n: int
) -> float:
    _check_counts(baseline_successes, baseline_n)
    _check_counts(current_successes, current_n)

    passes = baseline_successes + current_successes
    total = baseline_n + current_n
    log_all = _log_choose(total, passes)
    low = max(0, passes - baseline_n)
    probability = 0.0
    for k in range(low, current_successes + 1):
        probability += math.exp(
            _log_choose(current_n, k) + _log_choose(baseline_n, passes - k) - log_all
        )
    return min(1.0, probability)


@dataclass(frozen=True)
class Comparison:
    verdict: Verdict
    baseline_rate: float
    current_rate: float
    # Baseline rate minus current rate: positive means the pass rate fell.
    drop: float
    # One-sided: how likely a drop this large is if nothing changed.
    p_value: float
    # For "needs_data": about how many more runs at the current rate it would
    # take for this drop to be statistically clear. None otherwise.
    runs_needed: int | None


# How many more runs in the current batch, passing at the rate seen so far,
# would make the drop significant against the same baseline. Returns None if
# there is no drop, or if it would take more than MAX_RUNS_NEEDED.
#
# An estimate, not a promise: it assumes the current rate holds up, and the
# true answer moves with each result. Found by doubling and then bisecting.
def runs_needed(
    baseline_successes: int,
    baseline_n: int,
    current_successes: int,
    current_n: int,
    alpha: float = DEFAULT_ALPHA,
) -> int | None:
    rate = current_successes / current_n
    if rate >= baseline_successes / baseline_n:
        return None

    def significant(extra: int) -> bool:
        passes = current_successes + round(extra * rate)
        return p_value_lower(baseline_successes, baseline_n, passes, current_n + extra) < alpha

    if significant(0):
        return 0
    high = 1
    while not significant(high):
        high *= 2
        if high > MAX_RUNS_NEEDED:
            return None
    low = high // 2
    while high - low > 1:
        middle = (low + high) // 2
        if significant(middle):
            high = middle
        else:
            low = middle
    return high


# Decides whether the current batch is a regression against a baseline, from
# the counts alone (passes and total runs for each).
#
# - ok: no drop, or one too small to matter (under `min_drop`).
# - regression: a drop of at least `min_drop` that is statistically clear
#   (one-sided p below `alpha`).
# - needs_data: a drop of at least `min_drop` that might be real but isn't
#   clear yet. Not an alert; it says how many more runs would settle it.
def compare(
    baseline_successes: int,
    baseline_n: int,
    current_successes: int,
    current_n: int,
    alpha: float = DEFAULT_ALPHA,
    min_drop: float = DEFAULT_MIN_DROP,
) -> Comparison:
    p_value = p_value_lower(baseline_successes, baseline_n, current_successes, current_n)
    baseline_rate = baseline_successes / baseline_n
    current_rate = current_successes / current_n
    drop = baseline_rate - current_rate

    if drop < min_drop:
        verdict: Verdict = "ok"
        needed = None
    elif p_value < alpha:
        verdict = "regression"
        needed = None
    else:
        verdict = "needs_data"
        needed = runs_needed(baseline_successes, baseline_n, current_successes, current_n, alpha)
    return Comparison(verdict, baseline_rate, current_rate, drop, p_value, needed)

"""Pure numeric helpers used by metrics, insula, beliefs and experiments.

No IO, no state, standard library only. Every function is deterministic.
Invalid input raises ValueError; callers that need fail-closed `None`
semantics (zscore, ewma) get them explicitly.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

_SQRT2 = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)
_SIGN_FLIP_MAX = 20


def _require(xs: Sequence[float], minimum: int, name: str) -> None:
    if len(xs) < minimum:
        raise ValueError(f"{name} needs at least {minimum} value(s), got {len(xs)}")


# --- location and spread ---------------------------------------------------


def mean(xs: Sequence[float]) -> float:
    """Arithmetic mean. Raises ValueError on empty input."""
    _require(xs, 1, "mean")
    return math.fsum(xs) / len(xs)


def sd(xs: Sequence[float], ddof: int = 1) -> float:
    """Standard deviation with `ddof` delta degrees of freedom."""
    if ddof < 0:
        raise ValueError("ddof must be >= 0")
    _require(xs, ddof + 1, "sd")
    m = mean(xs)
    ss = math.fsum((x - m) ** 2 for x in xs)
    return math.sqrt(ss / (len(xs) - ddof))


def median(xs: Sequence[float]) -> float:
    """Median; the mean of the two middle values for even lengths."""
    _require(xs, 1, "median")
    s = sorted(xs)
    mid = len(s) // 2
    if len(s) % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def mad(xs: Sequence[float]) -> float:
    """Raw median absolute deviation (no 1.4826 consistency factor)."""
    m = median(xs)
    return median([abs(x - m) for x in xs])


def percentile(xs: Sequence[float], p: float) -> float:
    """Percentile with linear interpolation between order statistics.

    Rank is p/100 * (n - 1), matching the common "linear" definition.
    """
    _require(xs, 1, "percentile")
    if not 0.0 <= p <= 100.0:
        raise ValueError("p must lie in [0, 100]")
    s = sorted(xs)
    rank = p / 100.0 * (len(s) - 1)
    lo = math.floor(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


# --- regression and series -------------------------------------------------


def ols(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float]:
    """Simple linear regression of ys on xs.

    Returns (slope, intercept, residual_sd). residual_sd uses n - 2 degrees
    of freedom; with exactly two points the fit is exact and it is 0.0.
    """
    if len(xs) != len(ys):
        raise ValueError("xs and ys must have the same length")
    _require(xs, 2, "ols")
    mx, my = mean(xs), mean(ys)
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    if sxx <= 0.0:
        raise ValueError("ols needs variance in xs")
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ssr = math.fsum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    dof = max(len(xs) - 2, 1)
    return slope, intercept, math.sqrt(ssr / dof)


def ewma(
    values: Sequence[float | None], n: int, seed_after: int = 0
) -> list[float | None]:
    """Exponentially weighted moving average with lambda = 2 / (n + 1).

    The first non-None value seeds the state. A None input yields a None
    output and leaves the state unchanged. Outputs stay None until
    `seed_after` non-None values have been seen.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    if seed_after < 0:
        raise ValueError("seed_after must be >= 0")
    lam = 2.0 / (n + 1)
    state: float | None = None
    seen = 0
    out: list[float | None] = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        state = float(v) if state is None else state + lam * (v - state)
        seen += 1
        out.append(state if seen >= seed_after else None)
    return out


def lag1_autocorr(xs: Sequence[float]) -> float:
    """Lag-1 autocorrelation r1 = sum((x_t - m)(x_t+1 - m)) / sum((x_t - m)^2)."""
    _require(xs, 2, "lag1_autocorr")
    m = mean(xs)
    dev = [x - m for x in xs]
    denom = math.fsum(d * d for d in dev)
    if denom <= 0.0:
        raise ValueError("lag1_autocorr needs a non-constant series")
    num = math.fsum(a * b for a, b in zip(dev, dev[1:]))
    return num / denom


def zscore(
    x: float | None, mu: float | None, sigma: float | None, cap: float = 3.0
) -> float | None:
    """(x - mu) / sigma clamped to [-cap, cap]; None on missing input or sigma <= 0."""
    if x is None or mu is None or sigma is None or sigma <= 0.0:
        return None
    z = (x - mu) / sigma
    return max(-cap, min(cap, z))


# --- normal distribution ---------------------------------------------------


def phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


_A = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
      1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
_B = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
      6.680131188771972e01, -1.328068155288572e01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
      -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
      3.754408661907416e00)
_P_LOW = 0.02425


def _poly(coeffs: Sequence[float], x: float) -> float:
    acc = 0.0
    for c in coeffs:
        acc = acc * x + c
    return acc


def _acklam_tail(p: float) -> float:
    q = math.sqrt(-2.0 * math.log(p))
    return _poly(_C, q) / (_poly(_D, q) * q + 1.0)


def _acklam_central(p: float) -> float:
    q = p - 0.5
    r = q * q
    return _poly(_A, r) * q / (_poly(_B, r) * r + 1.0)


def phi_inv(p: float) -> float:
    """Inverse standard normal CDF (Acklam) with one Halley refinement step."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must lie in (0, 1)")
    if p < _P_LOW:
        x = _acklam_tail(p)
    elif p > 1.0 - _P_LOW:
        x = -_acklam_tail(1.0 - p)
    else:
        x = _acklam_central(p)
    e = 0.5 * math.erfc(-x / _SQRT2) - p
    u = e * _SQRT2PI * math.exp(x * x / 2.0)
    return x - u / (1.0 + x * u / 2.0)


# --- Student t distribution ------------------------------------------------

_CF_EPS = 1e-15
_CF_TINY = 1e-300
_CF_MAXIT = 500


def _cf_clamp(v: float) -> float:
    return _CF_TINY if abs(v) < _CF_TINY else v


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 / _cf_clamp(1.0 - qab * x / qap)
    h = d
    for m in range(1, _CF_MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 / _cf_clamp(1.0 + aa * d)
        c = _cf_clamp(1.0 + aa / c)
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 / _cf_clamp(1.0 + aa * d)
        c = _cf_clamp(1.0 + aa / c)
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _CF_EPS:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) for x in [0, 1]."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                 + a * math.log(x) + b * math.log1p(-x))
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _t_upper_tail(t: float, df: int) -> float:
    """P(T > t) for t >= 0."""
    x = df / (df + t * t)
    return 0.5 * _betainc(df / 2.0, 0.5, x)


def _t_ppf_upper(p: float, df: int) -> float:
    """Quantile for p > 0.5 by bracketing and bisection on the upper tail."""
    target = 1.0 - p
    lo, hi = 0.0, 1.0
    while _t_upper_tail(hi, df) > target:
        lo, hi = hi, hi * 2.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _t_upper_tail(mid, df) > target:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-12 * max(1.0, hi):
            break
    return 0.5 * (lo + hi)


def t_ppf(p: float, df: int) -> float:
    """Inverse CDF of Student's t with `df` >= 1 degrees of freedom."""
    if df < 1:
        raise ValueError("df must be >= 1")
    if not 0.0 < p < 1.0:
        raise ValueError("p must lie in (0, 1)")
    if p == 0.5:
        return 0.0
    if p < 0.5:
        return -_t_ppf_upper(1.0 - p, df)
    return _t_ppf_upper(p, df)


# --- tests and multiplicity -------------------------------------------------


def _signed_sums(d: Sequence[float]) -> list[float]:
    sums = [0.0]
    for v in d:
        sums = [s + v for s in sums] + [s - v for s in sums]
    return sums


def sign_flip_pvalue(d: Sequence[float]) -> float:
    """Exact two-sided sign-flip (randomization) p-value for pair differences.

    p = #{sign vectors s : |sum s_j d_j| >= |sum d_j|} / 2^m, enumerating all
    2^m sign vectors. Refuses m > 20.
    """
    if len(d) > _SIGN_FLIP_MAX:
        raise ValueError(f"sign_flip_pvalue supports at most {_SIGN_FLIP_MAX} values")
    observed = abs(math.fsum(d))
    tol = 1e-9 * max(1.0, math.fsum(abs(v) for v in d))
    sums = _signed_sums(d)
    hits = sum(1 for s in sums if abs(s) >= observed - tol)
    return hits / len(sums)


def mc_pvalue(observed: float, null_draws: Iterable[float]) -> float:
    """Monte Carlo two-sided p-value (1 + #{|x| >= |observed|}) / (1 + n)."""
    ref = abs(observed)
    n = 0
    hits = 0
    for x in null_draws:
        n += 1
        if abs(x) >= ref:
            hits += 1
    return (1 + hits) / (1 + n)


def benjamini_hochberg(pvals: Sequence[float], q: float = 0.1) -> list[bool]:
    """Benjamini-Hochberg step-up procedure; True marks a rejected hypothesis."""
    if not 0.0 < q <= 1.0:
        raise ValueError("q must lie in (0, 1]")
    if any(not 0.0 <= p <= 1.0 for p in pvals):
        raise ValueError("p-values must lie in [0, 1]")
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    cutoff = 0
    for rank, idx in enumerate(order, start=1):
        if pvals[idx] <= rank / m * q:
            cutoff = rank
    rejected = [False] * m
    for idx in order[:cutoff]:
        rejected[idx] = True
    return rejected


# --- Bayesian update ---------------------------------------------------------


def normal_update(mu0: float, sd0: float, est: float, se: float) -> tuple[float, float]:
    """Normal-normal conjugate update; returns the posterior (mu1, sd1)."""
    if sd0 <= 0.0 or se <= 0.0:
        raise ValueError("sd0 and se must be > 0")
    w0 = 1.0 / (sd0 * sd0)
    w1 = 1.0 / (se * se)
    prec = w0 + w1
    mu1 = (w0 * mu0 + w1 * est) / prec
    return mu1, math.sqrt(1.0 / prec)

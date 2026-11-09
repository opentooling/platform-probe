"""Durations in the "10s", "1m30s", "500ms" format the chart's values use (Go's time.Duration
format, as Kubernetes and Gateway API use it), parsed and printed the same way."""

import re

_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
_PART = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(ns|us|µs|μs|ms|s|m|h)")


def parse_duration(s: str) -> float:
    """Parse a duration such as "1m30s" into seconds. Raises ValueError on anything else."""
    text = s.strip()
    sign = 1.0
    if text[:1] in "+-" and text:
        sign = -1.0 if text[0] == "-" else 1.0
        text = text[1:]
    if text == "0":
        return 0.0
    if not text:
        raise ValueError(f"invalid duration {s!r}")
    total, pos = 0.0, 0
    while pos < len(text):
        m = _PART.match(text, pos)
        if not m:
            raise ValueError(f"invalid duration {s!r}")
        total += float(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    return sign * total


def _trim(x: float) -> str:
    out = f"{x:.9f}".rstrip("0").rstrip(".")
    return out or "0"


def format_duration(seconds: float) -> str:
    """Format seconds the way Go prints a time.Duration: "2s", "1m0s", "1.5s", "250ms"."""
    ns = round(seconds * 1e9)
    if ns == 0:
        return "0s"
    sign = "-" if ns < 0 else ""
    ns = abs(ns)
    if ns < 1_000_000_000:
        for unit, size in (("ms", 1_000_000), ("µs", 1_000), ("ns", 1)):
            if ns >= size:
                return sign + _trim(ns / size) + unit
    hours, rem = divmod(ns, 3_600_000_000_000)
    minutes, rem = divmod(rem, 60_000_000_000)
    secs = _trim(rem / 1e9) + "s"
    if hours:
        return f"{sign}{hours}h{minutes}m{secs}"
    if minutes:
        return f"{sign}{minutes}m{secs}"
    return sign + secs


def rounded(seconds: float, step: float = 0.001) -> str:
    """format_duration after rounding to a step, for details like "504 after 2.003s"."""
    return format_duration(round(seconds / step) * step)

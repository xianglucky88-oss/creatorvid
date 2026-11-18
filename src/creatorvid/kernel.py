"""Count normalization with explainable, stable semantics.

A play count may arrive in many surface formats ("1e6", "1000000",
"1,000,000").  Only the *canonical* value carries meaning, so a pure
display-format change must never look like real growth.  Every record is
annotated with the original expression, the source scale and the reason
for the decision, and anomalous counts are quarantined (canonical None)
instead of being silently folded to zero.
"""
import math
import re

RULE_VERSION = "count-v2"

# Keys this module adds to a record.  Record identity (framework.identity)
# excludes exactly these fields, so a rule update can never duplicate or
# silently rewrite already-stored history.  Keep this set in sync with
# process() output when changing the rules.
OUTPUT_FIELDS = frozenset({
    "value_raw", "value_norm", "scale_factor", "was_large",
    "value_canonical", "count_class", "count_reason", "count_rule",
})

# Classes whose value is excluded from counting (value_canonical is None).
QUARANTINED = frozenset({"empty", "invalid", "negative", "drift"})

LARGE_THRESHOLD = 1_000_000   # counts at/above this are legitimate "large"
DRIFT_THRESHOLD = 10 ** 15    # counts at/above this are abnormal drift

_EMPTY_TOKENS = {"", "-", "--", "null", "none", "n/a", "na", "unknown"}
_SCI_RE = re.compile(r"[0-9][eE][+-]?[0-9]")

_REASONS = {
    "empty": "missing count expression; excluded from counting",
    "invalid": "unparseable count expression; excluded from counting",
    "zero": "exact zero count",
    "negative": "negative count is implausible; excluded from counting",
    "drift": "count exceeds the plausible bound; excluded as abnormal drift",
    "scientific": "scientific-notation expression folded to its integer value",
    "large": "large count within the plausible bound",
    "normal": "plain count",
}


def _parse(raw):
    """Return (status, number, scientific) for a raw count expression.

    status is "ok", "empty" or "invalid"; number is always a finite float
    when status is "ok".
    """
    if raw is None:
        return "empty", None, False
    if isinstance(raw, bool):
        return "invalid", None, False
    if isinstance(raw, (int, float)):
        v = float(raw)
        if not math.isfinite(v):
            return "invalid", None, False
        return "ok", v, False
    s = str(raw).strip()
    if s.lower() in _EMPTY_TOKENS:
        return "empty", None, False
    t = s.replace(",", "").replace("_", "")
    try:
        v = float(t)
    except ValueError:
        return "invalid", None, False
    if not math.isfinite(v):
        return "invalid", None, False
    return "ok", v, bool(_SCI_RE.search(t))


def _classify(status, v, scientific):
    """Return (count_class, value_canonical)."""
    if status != "ok":
        return status, None
    if v == 0:
        return "zero", 0
    if v < 0:
        return "negative", None
    if v >= DRIFT_THRESHOLD:
        return "drift", None
    canon = int(v) if v.is_integer() else v
    if scientific:
        return "scientific", canon
    if v >= LARGE_THRESHOLD:
        return "large", canon
    return "normal", canon


def process(record):
    raw = record.get("value")
    status, v, scientific = _parse(raw)
    cls, canonical = _classify(status, v, scientific)

    # Legacy source-scale view (kept for existing consumers): express the
    # magnitude in thousands-based units.
    norm = v if v is not None else 0.0
    scale = 1
    while abs(norm) >= 1e6 and scale <= 10 ** 9:
        norm /= 1000.0
        scale *= 1000

    return {
        "value_raw": None if raw is None else str(raw),
        "value_norm": round(norm, 4),
        "scale_factor": scale,
        "was_large": int(scale > 1),
        "value_canonical": canonical,
        "count_class": cls,
        "count_reason": _REASONS[cls],
        "count_rule": RULE_VERSION,
    }

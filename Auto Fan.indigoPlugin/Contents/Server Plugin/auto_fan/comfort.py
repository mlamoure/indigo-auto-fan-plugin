"""Comfort offsets: what a manual fan change says about the ideal temperature.

A person who raises the fan is telling us the room feels warmer than the
ideal we are using. Running the season's speed curve backwards from the step
they chose gives the temperature difference that would have produced that
speed; subtracting it from the measured room temperature gives the ideal they
were acting on. The gap to the configured ideal is the *comfort offset*.

Two kinds exist:
- a **temporary** offset, derived from each manual change and held until the
  night window ends (change made at night) or begins (change made by day);
- a **standing** offset per season and day/night (Phase 5), set by hand or
  recommended by the learner.
"""
from typing import List, Optional, Tuple

OFFSET_LIMIT = 3.0       # degrees F, either direction
OFFSET_STEP = 0.5        # degrees F


def implied_delta_from_speed(speed_pct: float, points: List[dict]) -> Optional[float]:
    """Invert the speed curve: the temperature offset at which the curve
    produces ``speed_pct``.

    The curve is piecewise linear and held flat beyond its ends. Below the
    curve's lowest speed the answer is the leftmost offset; above its highest
    speed the rightmost. Within a flat segment the segment's midpoint is used.
    Returns None for an empty curve.
    """
    if not points:
        return None
    pts = sorted(points, key=lambda p: p["offset"])
    speeds = [float(p["speed"]) for p in pts]
    if speed_pct <= min(speeds):
        # the lowest offset at which the curve is already at its minimum
        for p in pts:
            if float(p["speed"]) == min(speeds):
                return float(p["offset"])
    if speed_pct >= max(speeds):
        # the lowest offset at which the curve reaches its maximum
        for p in pts:
            if float(p["speed"]) == max(speeds):
                return float(p["offset"])
    for a, b in zip(pts, pts[1:]):
        sa, sb = float(a["speed"]), float(b["speed"])
        lo, hi = min(sa, sb), max(sa, sb)
        if lo <= speed_pct <= hi:
            if sa == sb:
                return (float(a["offset"]) + float(b["offset"])) / 2.0
            t = (speed_pct - sa) / (sb - sa)
            return float(a["offset"]) + t * (float(b["offset"]) - float(a["offset"]))
    return None


def round_offset(value: float, limit: float = OFFSET_LIMIT, step: float = OFFSET_STEP) -> float:
    """Round to the nearest step and clamp to ±limit."""
    rounded = round(value / step) * step
    return max(-limit, min(limit, rounded))


def temporary_offset_for_change(human_pct: float, curve_points: List[dict],
                                room_temp: Optional[float], configured_ideal: Optional[float],
                                additive_boost: float = 0.0) -> Optional[Tuple[float, float]]:
    """Derive (implied_ideal, offset) from a manual change.

    ``additive_boost`` is the sum of the additive modifiers that were active
    (HVAC boost/adjust, humidity boost); it is removed first so the curve is
    inverted on the base speed the person effectively chose.
    """
    if room_temp is None or configured_ideal is None:
        return None
    base_pct = max(0.0, min(100.0, float(human_pct) - float(additive_boost)))
    delta = implied_delta_from_speed(base_pct, curve_points)
    if delta is None:
        return None
    implied_ideal = room_temp - delta
    offset = round_offset(implied_ideal - configured_ideal)
    if not offset_is_informative(base_pct, offset, curve_points):
        return None
    return implied_ideal, offset


def curve_speed_range(points: List[dict]) -> Optional[Tuple[float, float]]:
    """(lowest, highest) speed of a curve, or None for an empty curve."""
    if not points:
        return None
    speeds = [float(p["speed"]) for p in points]
    return min(speeds), max(speeds)


def offset_is_informative(speed_pct: Optional[float], offset: Optional[float], points: List[dict]) -> bool:
    """Whether a change to ``speed_pct`` says anything about the ideal.

    At or below the curve's lowest speed the curve is flat, so a person who
    chose that speed (or turned the fan off) only tells us the ideal is *at
    least* the inverted value: the room may be far colder than the curve's
    end. That bound is evidence for a warmer ideal when it lies above the one
    in use, and no evidence at all when it lies below; read as a value it
    would say "cooler" about a person who turned the fan off in a cold room.
    The same holds the other way round at the curve's highest speed.
    """
    if speed_pct is None or offset is None:
        return True
    rng = curve_speed_range(points)
    if rng is None:
        return True
    lowest, highest = rng
    if float(speed_pct) <= lowest:
        return offset > 0
    if float(speed_pct) >= highest:
        return offset < 0
    return True

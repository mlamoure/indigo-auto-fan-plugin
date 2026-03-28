from datetime import time, datetime
from typing import List, Optional, Tuple


def interpolate(value: float, points: List[dict]) -> float:
    """
    Linear interpolation between control points.

    Args:
        value: The input value (temperature offset from target).
        points: List of {"offset": float, "speed": float}.

    Returns:
        Interpolated speed percentage (0-100).
    """
    if not points:
        return 0.0

    pts = sorted(points, key=lambda p: p["offset"])

    # Clamp below lowest point
    if value <= pts[0]["offset"]:
        return pts[0]["speed"]

    # Clamp above highest point
    if value >= pts[-1]["offset"]:
        return pts[-1]["speed"]

    # Find bracket and interpolate
    for i in range(len(pts) - 1):
        if pts[i]["offset"] <= value <= pts[i + 1]["offset"]:
            span = pts[i + 1]["offset"] - pts[i]["offset"]
            if span == 0:
                return pts[i]["speed"]
            t = (value - pts[i]["offset"]) / span
            return pts[i]["speed"] + t * (pts[i + 1]["speed"] - pts[i]["speed"])

    return 0.0


def calculate_base_speed(delta: float, fan_curve: dict) -> float:
    """
    Interpolate base fan speed from the unified fan curve.

    Args:
        delta: Temperature delta (current - ideal). Positive = warmer than target.
        fan_curve: {"points": [...]} with offset/speed pairs.

    Returns:
        Interpolated speed percentage (0-100).
    """
    points = fan_curve.get("points", [])
    return interpolate(delta, points)


def _is_nighttime(start_hour: int, end_hour: int) -> bool:
    """Check if current time is within nighttime hours."""
    now = datetime.now().time()
    start = time(start_hour, 0)
    end = time(end_hour, 0)

    if start > end:
        # Crosses midnight (e.g., 22:00 to 08:00)
        return now >= start or now < end
    else:
        return start <= now < end


# Modifier application order matters: additive modifiers (HVAC, humidity) run first,
# then clamps (nighttime, presence). This prevents clamps from being circumvented by
# later additive adjustments — e.g., nighttime caps the speed, then no further boost
# can push it back above the cap.
def apply_modifiers(
    base_speed: float,
    modifiers: dict,
    is_hvac_cooling: bool,
    is_hvac_heating: bool,
    humidity: Optional[float],
    is_home: bool,
) -> Tuple[float, List[Tuple[str, str]]]:
    """
    Apply modifier stack to base speed.

    Modifier order:
    1. HVAC cooling active: additive boost + min clamp
    2. HVAC heating active: additive adjustment (+ or -) + min clamp
    3. Humidity: flat boost when above threshold
    4. Nighttime: clamp to range
    5. Away: cap speed when not home
    6. Final clamp to 0-100

    Modifiers are implicitly disabled when their primary value is at the
    neutral position (e.g., speed_boost_pct=0, clamp_max_pct=100).

    Args:
        base_speed: Speed from curve interpolation.
        modifiers: Modifier config dict from zone config.
        is_hvac_cooling: Whether HVAC is actively cooling.
        is_hvac_heating: Whether HVAC is actively heating.
        humidity: Current humidity percentage (None if unavailable).
        is_home: Whether someone is home (from global away variable).

    Returns:
        Tuple of (final_speed_pct, list of (emoji, reason) contributions).
    """
    speed = base_speed
    contributions: List[Tuple[str, str]] = []

    # 1. HVAC cooling modifier
    # Boost and clamp_min are checked separately so each gets its own log entry,
    # and clamp_min works even without a boost (e.g., "always run at least 30%
    # when AC is on").
    hvac_cool = modifiers.get("hvac_cooling_active", {})
    if is_hvac_cooling:
        boost = hvac_cool.get("speed_boost_pct", 0)
        clamp_min = hvac_cool.get("clamp_min_pct", 0)
        if boost:
            speed += boost
            contributions.append(("❄️", f"HVAC cooling active: +{boost}%"))
        if clamp_min and speed < clamp_min:
            speed = clamp_min
            contributions.append(("❄️", f"HVAC cooling min: {clamp_min}%"))

    # 2. HVAC heating modifier
    # speed_adjust_pct supports both positive (circulate warm air via reverse mode)
    # and negative (reduce airflow during heating) values.
    hvac_heat = modifiers.get("hvac_heating_active", {})
    if is_hvac_heating:
        adj = hvac_heat.get("speed_adjust_pct", 0)
        clamp_min = hvac_heat.get("clamp_min_pct", 0)
        if adj:
            speed += adj
            contributions.append(("🔥", f"HVAC heating active: {adj:+}%"))
        if clamp_min and speed < clamp_min:
            speed = clamp_min
            contributions.append(("🔥", f"HVAC heating min: {clamp_min}%"))

    # 3. Humidity modifier
    # Flat boost: full speed_boost_pct applied when humidity exceeds threshold.
    # Simpler than the previous proportional model and matches the dropdown UI.
    hum_mod = modifiers.get("humidity", {})
    if humidity is not None:
        boost = hum_mod.get("speed_boost_pct", 0)
        threshold = hum_mod.get("threshold", 60)
        if boost and humidity > threshold:
            speed += boost
            contributions.append(
                ("💧", f"Humidity {humidity:.0f}% (>{threshold}%): +{boost}%")
            )

    # 4. Nighttime clamp
    # Neutral values (min=0, max=100) implicitly disable this modifier,
    # avoiding the need for a separate "enabled" flag.
    night_mod = modifiers.get("nighttime", {})
    clamp_min = night_mod.get("clamp_min_pct", 0)
    clamp_max = night_mod.get("clamp_max_pct", 100)
    if clamp_max < 100 or clamp_min > 0:
        start_hour = night_mod.get("night_start_hour", 22)
        end_hour = night_mod.get("night_end_hour", 8)
        if _is_nighttime(start_hour, end_hour):
            old_speed = speed
            speed = max(clamp_min, min(speed, clamp_max))
            if speed != old_speed:
                contributions.append(
                    ("🌙", f"Nighttime clamp: [{clamp_min}-{clamp_max}%]")
                )

    # 5. Away cap
    # clamp_max_pct=100 implicitly disables (no cap).
    away_mod = modifiers.get("away", {})
    cap = away_mod.get("clamp_max_pct", 100)
    if not is_home and cap < 100:
        old_speed = speed
        speed = min(speed, cap)
        if speed != old_speed:
            contributions.append(("🏠", f"Away: capped at {cap}%"))

    # 6. Final clamp
    speed = max(0.0, min(100.0, speed))

    return speed, contributions

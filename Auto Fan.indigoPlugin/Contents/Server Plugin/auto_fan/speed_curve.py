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
    has_presence: bool,
) -> Tuple[float, List[Tuple[str, str]]]:
    """
    Apply modifier stack to base speed.

    Modifier order:
    1. HVAC cooling active: additive boost
    2. HVAC heating active: additive reduction + clamp
    3. Humidity: additive boost based on excess over threshold
    4. Nighttime: clamp to range
    5. No presence: cap speed
    6. Final clamp to 0-100

    Args:
        base_speed: Speed from curve interpolation.
        modifiers: Modifier config dict from zone config.
        is_hvac_cooling: Whether HVAC is actively cooling.
        is_hvac_heating: Whether HVAC is actively heating.
        humidity: Current humidity percentage (None if unavailable).
        has_presence: Whether presence is detected in the zone.

    Returns:
        Tuple of (final_speed_pct, list of (emoji, reason) contributions).
    """
    speed = base_speed
    contributions: List[Tuple[str, str]] = []

    # 1. HVAC cooling modifier
    hvac_cool = modifiers.get("hvac_cooling_active", {})
    if hvac_cool.get("enabled", False) and is_hvac_cooling:
        adj = hvac_cool.get("speed_adjust_pct", 0)
        speed += adj
        contributions.append(("❄️", f"HVAC cooling active: +{adj}%"))

    # 2. HVAC heating modifier
    hvac_heat = modifiers.get("hvac_heating_active", {})
    if hvac_heat.get("enabled", False) and is_hvac_heating:
        adj = hvac_heat.get("speed_adjust_pct", 0)
        speed += adj
        clamp_min = hvac_heat.get("clamp_min_pct", 0)
        speed = max(speed, clamp_min)
        contributions.append(("🔥", f"HVAC heating active: {adj:+}%"))

    # 3. Humidity modifier
    hum_mod = modifiers.get("humidity", {})
    if hum_mod.get("enabled", False) and humidity is not None:
        threshold = hum_mod.get("threshold", 60)
        if humidity > threshold:
            excess = humidity - threshold
            per_unit = hum_mod.get("speed_adjust_per_unit_pct", 0.5)
            max_adj = hum_mod.get("max_adjust_pct", 15)
            adj = min(excess * per_unit, max_adj)
            speed += adj
            contributions.append(
                ("💧", f"Humidity {humidity:.0f}% (>{threshold}%): +{adj:.1f}%")
            )

    # 4. Nighttime clamp
    night_mod = modifiers.get("nighttime", {})
    if night_mod.get("enabled", False):
        start_hour = night_mod.get("night_start_hour", 22)
        end_hour = night_mod.get("night_end_hour", 8)
        if _is_nighttime(start_hour, end_hour):
            clamp_min = night_mod.get("clamp_min_pct", 0)
            clamp_max = night_mod.get("clamp_max_pct", 50)
            old_speed = speed
            speed = max(clamp_min, min(speed, clamp_max))
            if speed != old_speed:
                contributions.append(
                    ("🌙", f"Nighttime clamp: [{clamp_min}-{clamp_max}%]")
                )

    # 5. No presence cap
    no_pres = modifiers.get("no_presence", {})
    if no_pres.get("enabled", False) and not has_presence:
        cap = no_pres.get("clamp_max_pct", 0)
        old_speed = speed
        speed = min(speed, cap)
        if speed != old_speed:
            contributions.append(("👤", f"No presence: capped at {cap}%"))

    # 6. Final clamp
    speed = max(0.0, min(100.0, speed))

    return speed, contributions

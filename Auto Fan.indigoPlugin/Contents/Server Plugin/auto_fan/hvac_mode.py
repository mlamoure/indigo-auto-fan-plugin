from enum import Enum


class HvacMode(Enum):
    WINTER = "winter"
    SUMMER_COOLING = "summer_cooling"
    TRANSITIONAL = "transitional"
    NEUTRAL = "neutral"


# Priority order: Winter checked first because heat-only (heat active, no cool
# setpoint) is unambiguous. Summer cooling checked before transitional because a
# warm outdoor temp is a stronger signal than both setpoints being active — in
# shoulder seasons, both may be set but the system is effectively cooling.
def detect_hvac_mode(heat_setpoint, cool_setpoint, outdoor_temp, ideal_temp) -> HvacMode:
    """
    Infer HVAC mode from thermostat setpoints and outdoor temperature.

    Rules (evaluated in order):
    1. Heat setpoint > 50 AND cool setpoint <= 0 (or None) -> WINTER
    2. Cool setpoint > 0 AND (outdoor > ideal or room is warm) -> SUMMER_COOLING
    3. Both heat > 50 AND cool > 0 -> TRANSITIONAL
    4. Neither active -> NEUTRAL

    Args:
        heat_setpoint: Thermostat heat setpoint (None if not set / < 0 means off)
        cool_setpoint: Thermostat cool setpoint (None if not set / < 0 means off)
        outdoor_temp: Current outdoor temperature (None if unavailable)
        ideal_temp: Zone ideal temperature for comparison

    Returns:
        HvacMode enum value
    """
    heat_active = heat_setpoint is not None and heat_setpoint > 50
    cool_active = cool_setpoint is not None and cool_setpoint > 0

    # Winter: heating only, no cooling
    if heat_active and not cool_active:
        return HvacMode.WINTER

    # Summer cooling: cooling active and it's warm outside
    if cool_active:
        if outdoor_temp is not None and ideal_temp is not None and outdoor_temp > ideal_temp:
            return HvacMode.SUMMER_COOLING
        # If we can't check outdoor temp, assume summer cooling if cool is active
        if outdoor_temp is None:
            return HvacMode.SUMMER_COOLING

    # Transitional: both heat and cool setpoints active
    if heat_active and cool_active:
        return HvacMode.TRANSITIONAL

    return HvacMode.NEUTRAL

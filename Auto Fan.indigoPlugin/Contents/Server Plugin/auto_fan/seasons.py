from datetime import datetime
from typing import Optional

SEASONS = ("spring", "summer", "fall", "winter")

SEASON_LABELS = {
    "spring": "Spring",
    "summer": "Summer",
    "fall": "Fall",
    "winter": "Winter",
}

# Month-to-season mappings by hemisphere
_NORTH_MONTH_TO_SEASON = {
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "fall", 10: "fall", 11: "fall",
    12: "winter", 1: "winter", 2: "winter",
}

_SOUTH_MONTH_TO_SEASON = {
    3: "fall", 4: "fall", 5: "fall",
    6: "winter", 7: "winter", 8: "winter",
    9: "spring", 10: "spring", 11: "spring",
    12: "summer", 1: "summer", 2: "summer",
}


def get_current_season(
    now: Optional[datetime] = None,
    mode: str = "automatic",
    hemisphere: str = "north",
    season_var_id: Optional[int] = None,
) -> str:
    """Return current season based on detection mode.

    Args:
        now: Override datetime (for testing). Only used in automatic mode.
        mode: "automatic" (month + hemisphere) or "variable" (Indigo variable).
        hemisphere: "north" or "south". Only used in automatic mode.
        season_var_id: Indigo variable ID containing season string.
            Only used in variable mode.

    Returns:
        One of: "spring", "summer", "fall", "winter".
    """
    if mode == "variable" and season_var_id is not None:
        try:
            import indigo  # noqa: F811 — runtime import for Indigo environment
            val = str(indigo.variables[season_var_id].value).strip().lower()
            if val in SEASONS:
                return val
        except Exception:
            pass
        # Fall through to automatic on failure

    month = (now or datetime.now()).month
    season_map = _SOUTH_MONTH_TO_SEASON if hemisphere == "south" else _NORTH_MONTH_TO_SEASON
    return season_map.get(month, "summer")

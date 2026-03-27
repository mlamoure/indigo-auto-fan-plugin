from datetime import datetime
from typing import Optional

SEASONS = ("spring", "summer", "fall", "winter")

SEASON_LABELS = {
    "spring": "Spring",
    "summer": "Summer",
    "fall": "Fall",
    "winter": "Winter",
}


def get_current_season(now: Optional[datetime] = None) -> str:
    """Return current meteorological season based on month.

    Spring: March-May, Summer: June-August,
    Fall: September-November, Winter: December-February.
    """
    month = (now or datetime.now()).month
    if month in (3, 4, 5):
        return "spring"
    elif month in (6, 7, 8):
        return "summer"
    elif month in (9, 10, 11):
        return "fall"
    else:
        return "winter"

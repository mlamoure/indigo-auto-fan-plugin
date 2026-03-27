"""Tests for season determination."""
from datetime import datetime
from auto_fan.seasons import get_current_season, SEASONS


class TestGetCurrentSeason:
    def test_spring_months(self):
        for month in (3, 4, 5):
            assert get_current_season(datetime(2026, month, 15)) == "spring"

    def test_summer_months(self):
        for month in (6, 7, 8):
            assert get_current_season(datetime(2026, month, 15)) == "summer"

    def test_fall_months(self):
        for month in (9, 10, 11):
            assert get_current_season(datetime(2026, month, 15)) == "fall"

    def test_winter_months(self):
        for month in (12, 1, 2):
            assert get_current_season(datetime(2026, month, 15)) == "winter"

    def test_seasons_constant(self):
        assert SEASONS == ("spring", "summer", "fall", "winter")

    def test_defaults_to_now(self):
        result = get_current_season()
        assert result in SEASONS

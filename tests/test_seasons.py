"""Tests for season determination."""
from datetime import datetime
from unittest.mock import patch, MagicMock

from auto_fan.seasons import get_current_season, SEASONS


class TestGetCurrentSeason:
    def test_spring_months_north(self):
        for month in (3, 4, 5):
            assert get_current_season(datetime(2026, month, 15)) == "spring"

    def test_summer_months_north(self):
        for month in (6, 7, 8):
            assert get_current_season(datetime(2026, month, 15)) == "summer"

    def test_fall_months_north(self):
        for month in (9, 10, 11):
            assert get_current_season(datetime(2026, month, 15)) == "fall"

    def test_winter_months_north(self):
        for month in (12, 1, 2):
            assert get_current_season(datetime(2026, month, 15)) == "winter"

    def test_spring_months_south(self):
        for month in (9, 10, 11):
            assert get_current_season(datetime(2026, month, 15), hemisphere="south") == "spring"

    def test_summer_months_south(self):
        for month in (12, 1, 2):
            assert get_current_season(datetime(2026, month, 15), hemisphere="south") == "summer"

    def test_fall_months_south(self):
        for month in (3, 4, 5):
            assert get_current_season(datetime(2026, month, 15), hemisphere="south") == "fall"

    def test_winter_months_south(self):
        for month in (6, 7, 8):
            assert get_current_season(datetime(2026, month, 15), hemisphere="south") == "winter"

    def test_seasons_constant(self):
        assert SEASONS == ("spring", "summer", "fall", "winter")

    def test_defaults_to_now(self):
        result = get_current_season()
        assert result in SEASONS

    def test_variable_mode_valid(self):
        mock_var = MagicMock()
        mock_var.value = "winter"
        mock_indigo = MagicMock()
        mock_indigo.variables = {10: mock_var}

        with patch.dict("sys.modules", {"indigo": mock_indigo}):
            result = get_current_season(mode="variable", season_var_id=10)
            assert result == "winter"

    def test_variable_mode_invalid_value_falls_through(self):
        """Invalid variable value falls through to automatic."""
        mock_var = MagicMock()
        mock_var.value = "not_a_season"
        mock_indigo = MagicMock()
        mock_indigo.variables = {10: mock_var}

        with patch.dict("sys.modules", {"indigo": mock_indigo}):
            result = get_current_season(
                now=datetime(2026, 7, 15), mode="variable", season_var_id=10
            )
            assert result == "summer"  # Falls through to automatic

    def test_variable_mode_missing_var_falls_through(self):
        """Missing variable falls through to automatic."""
        result = get_current_season(
            now=datetime(2026, 1, 15), mode="variable", season_var_id=999
        )
        assert result == "winter"

    def test_variable_mode_no_var_id(self):
        """No var_id with variable mode falls through to automatic."""
        result = get_current_season(
            now=datetime(2026, 6, 15), mode="variable", season_var_id=None
        )
        assert result == "summer"

    def test_hemisphere_default_is_north(self):
        assert get_current_season(datetime(2026, 7, 15)) == "summer"
        assert get_current_season(datetime(2026, 7, 15), hemisphere="north") == "summer"
        assert get_current_season(datetime(2026, 7, 15), hemisphere="south") == "winter"

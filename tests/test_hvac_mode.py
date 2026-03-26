"""Tests for HVAC mode auto-detection."""
from auto_fan.hvac_mode import HvacMode, detect_hvac_mode


class TestDetectHvacMode:
    """Tests for detect_hvac_mode()."""

    def test_winter_heat_only(self):
        """Heat > 50, no cooling -> WINTER."""
        assert detect_hvac_mode(68, -1, 30, 72) == HvacMode.WINTER

    def test_winter_cool_none(self):
        """Heat > 50, cool is None -> WINTER."""
        assert detect_hvac_mode(68, None, 30, 72) == HvacMode.WINTER

    def test_summer_cooling_outdoor_warm(self):
        """Cool > 0, outdoor > ideal -> SUMMER_COOLING."""
        assert detect_hvac_mode(None, 76, 85, 72) == HvacMode.SUMMER_COOLING

    def test_summer_cooling_no_outdoor_temp(self):
        """Cool > 0, outdoor temp unknown -> SUMMER_COOLING (assume cooling needed)."""
        assert detect_hvac_mode(None, 76, None, 72) == HvacMode.SUMMER_COOLING

    def test_transitional_both_setpoints(self):
        """Both heat > 50 and cool > 0 with outdoor cooler than ideal -> TRANSITIONAL."""
        assert detect_hvac_mode(65, 78, 60, 72) == HvacMode.TRANSITIONAL

    def test_neutral_nothing_active(self):
        """Neither heating nor cooling -> NEUTRAL."""
        assert detect_hvac_mode(None, None, 70, 72) == HvacMode.NEUTRAL

    def test_neutral_heat_below_50(self):
        """Heat setpoint too low, no cooling -> NEUTRAL."""
        assert detect_hvac_mode(45, None, 70, 72) == HvacMode.NEUTRAL

    def test_neutral_cool_at_zero(self):
        """Cool setpoint at 0 -> not considered active."""
        assert detect_hvac_mode(None, 0, 80, 72) == HvacMode.NEUTRAL

    def test_summer_beats_transitional_when_warm(self):
        """Both setpoints active + outdoor warm -> SUMMER_COOLING (checked before transitional)."""
        assert detect_hvac_mode(65, 76, 85, 72) == HvacMode.SUMMER_COOLING

    def test_winter_beats_all_when_heat_only(self):
        """Heat active + cool inactive -> WINTER regardless of outdoor temp."""
        assert detect_hvac_mode(68, -1, 90, 72) == HvacMode.WINTER

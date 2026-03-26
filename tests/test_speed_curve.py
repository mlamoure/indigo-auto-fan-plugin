"""Tests for speed curve interpolation and modifier stacking."""
import pytest
from unittest.mock import patch
from datetime import datetime, time
from auto_fan.speed_curve import interpolate, calculate_base_speed, apply_modifiers, _is_nighttime


class TestInterpolate:
    """Tests for the breakpoint interpolation function."""

    def test_empty_breakpoints_returns_zero(self):
        assert interpolate(5.0, []) == 0.0

    def test_single_breakpoint_returns_its_value(self):
        bp = [{"delta": 0, "speed_pct": 50}]
        assert interpolate(0, bp) == 50
        assert interpolate(-5, bp) == 50
        assert interpolate(10, bp) == 50

    def test_clamp_below_lowest(self):
        bp = [{"delta": 0, "speed_pct": 10}, {"delta": 5, "speed_pct": 80}]
        assert interpolate(-3, bp) == 10

    def test_clamp_above_highest(self):
        bp = [{"delta": 0, "speed_pct": 10}, {"delta": 5, "speed_pct": 80}]
        assert interpolate(10, bp) == 80

    def test_exact_breakpoint_value(self):
        bp = [{"delta": 0, "speed_pct": 0}, {"delta": 5, "speed_pct": 100}]
        assert interpolate(0, bp) == 0
        assert interpolate(5, bp) == 100

    def test_midpoint_interpolation(self):
        bp = [{"delta": 0, "speed_pct": 0}, {"delta": 10, "speed_pct": 100}]
        assert interpolate(5, bp) == pytest.approx(50.0)

    def test_quarter_interpolation(self):
        bp = [{"delta": 0, "speed_pct": 0}, {"delta": 8, "speed_pct": 100}]
        assert interpolate(2, bp) == pytest.approx(25.0)

    def test_multi_segment_interpolation(self):
        bp = [
            {"delta": 0, "speed_pct": 0},
            {"delta": 3, "speed_pct": 50},
            {"delta": 6, "speed_pct": 85},
            {"delta": 8, "speed_pct": 100},
        ]
        # Between 0 and 3
        assert interpolate(1.5, bp) == pytest.approx(25.0)
        # Between 3 and 6
        assert interpolate(4.5, bp) == pytest.approx(67.5)
        # Between 6 and 8
        assert interpolate(7, bp) == pytest.approx(92.5)

    def test_negative_deltas_warming_curve(self):
        bp = [
            {"delta": -4, "speed_pct": 20},
            {"delta": -2, "speed_pct": 10},
            {"delta": 0, "speed_pct": 0},
        ]
        assert interpolate(-3, bp) == pytest.approx(15.0)
        assert interpolate(-1, bp) == pytest.approx(5.0)
        assert interpolate(-5, bp) == 20  # clamped below

    def test_unsorted_breakpoints_still_work(self):
        bp = [
            {"delta": 5, "speed_pct": 100},
            {"delta": 0, "speed_pct": 0},
        ]
        assert interpolate(2.5, bp) == pytest.approx(50.0)


class TestCalculateBaseSpeed:
    """Tests for curve selection based on delta sign."""

    def test_positive_delta_uses_cooling_curve(self):
        cooling = {"breakpoints": [{"delta": 0, "speed_pct": 0}, {"delta": 5, "speed_pct": 100}]}
        warming = {"breakpoints": [{"delta": 0, "speed_pct": 0}, {"delta": -5, "speed_pct": 50}]}
        speed, curve = calculate_base_speed(3.0, cooling, warming)
        assert curve == "cooling"
        assert speed == pytest.approx(60.0)

    def test_negative_delta_uses_warming_curve(self):
        cooling = {"breakpoints": [{"delta": 0, "speed_pct": 0}, {"delta": 5, "speed_pct": 100}]}
        warming = {"breakpoints": [{"delta": 0, "speed_pct": 0}, {"delta": -5, "speed_pct": 50}]}
        speed, curve = calculate_base_speed(-2.5, cooling, warming)
        assert curve == "warming"
        assert speed == pytest.approx(25.0)

    def test_zero_delta_uses_cooling_curve(self):
        cooling = {"breakpoints": [{"delta": 0, "speed_pct": 15}]}
        warming = {"breakpoints": [{"delta": 0, "speed_pct": 0}]}
        speed, curve = calculate_base_speed(0.0, cooling, warming)
        assert curve == "cooling"
        assert speed == 15

    def test_empty_curves_return_zero(self):
        speed, curve = calculate_base_speed(5.0, {"breakpoints": []}, {"breakpoints": []})
        assert speed == 0.0


class TestApplyModifiers:
    """Tests for modifier stacking."""

    def test_no_modifiers_returns_base(self):
        speed, contribs = apply_modifiers(50.0, {}, False, False, None, True)
        assert speed == 50.0
        assert contribs == []

    def test_hvac_cooling_boost(self):
        mods = {"hvac_cooling_active": {"enabled": True, "speed_adjust_pct": 15}}
        speed, contribs = apply_modifiers(50.0, mods, True, False, None, True)
        assert speed == 65.0
        assert len(contribs) == 1
        assert "❄️" in contribs[0][0]

    def test_hvac_cooling_not_active(self):
        mods = {"hvac_cooling_active": {"enabled": True, "speed_adjust_pct": 15}}
        speed, contribs = apply_modifiers(50.0, mods, False, False, None, True)
        assert speed == 50.0
        assert contribs == []

    def test_hvac_heating_reduction(self):
        mods = {"hvac_heating_active": {"enabled": True, "speed_adjust_pct": -20, "clamp_min_pct": 0}}
        speed, contribs = apply_modifiers(50.0, mods, False, True, None, True)
        assert speed == 30.0

    def test_hvac_heating_clamp_min(self):
        mods = {"hvac_heating_active": {"enabled": True, "speed_adjust_pct": -80, "clamp_min_pct": 10}}
        speed, contribs = apply_modifiers(50.0, mods, False, True, None, True)
        assert speed == 10.0  # clamped to min, not -30

    def test_humidity_above_threshold(self):
        mods = {"humidity": {"enabled": True, "threshold": 60, "speed_adjust_per_unit_pct": 0.5, "max_adjust_pct": 15}}
        speed, contribs = apply_modifiers(50.0, mods, False, False, 75, True)
        # excess = 15, adj = 15 * 0.5 = 7.5
        assert speed == pytest.approx(57.5)

    def test_humidity_below_threshold(self):
        mods = {"humidity": {"enabled": True, "threshold": 60, "speed_adjust_per_unit_pct": 0.5, "max_adjust_pct": 15}}
        speed, contribs = apply_modifiers(50.0, mods, False, False, 55, True)
        assert speed == 50.0

    def test_humidity_capped_at_max_adjust(self):
        mods = {"humidity": {"enabled": True, "threshold": 60, "speed_adjust_per_unit_pct": 1.0, "max_adjust_pct": 10}}
        speed, contribs = apply_modifiers(50.0, mods, False, False, 90, True)
        # excess = 30, adj would be 30 but capped at 10
        assert speed == 60.0

    def test_no_presence_caps_speed(self):
        mods = {"no_presence": {"enabled": True, "clamp_max_pct": 0}}
        speed, contribs = apply_modifiers(75.0, mods, False, False, None, False)
        assert speed == 0.0

    def test_no_presence_not_triggered_when_present(self):
        mods = {"no_presence": {"enabled": True, "clamp_max_pct": 0}}
        speed, contribs = apply_modifiers(75.0, mods, False, False, None, True)
        assert speed == 75.0

    def test_final_clamp_to_zero(self):
        mods = {"hvac_heating_active": {"enabled": True, "speed_adjust_pct": -100, "clamp_min_pct": 0}}
        speed, contribs = apply_modifiers(20.0, mods, False, True, None, True)
        assert speed == 0.0

    def test_final_clamp_to_hundred(self):
        mods = {"hvac_cooling_active": {"enabled": True, "speed_adjust_pct": 80}}
        speed, contribs = apply_modifiers(80.0, mods, True, False, None, True)
        assert speed == 100.0  # clamped, not 160

    def test_multiple_modifiers_stack(self):
        mods = {
            "hvac_cooling_active": {"enabled": True, "speed_adjust_pct": 10},
            "humidity": {"enabled": True, "threshold": 60, "speed_adjust_per_unit_pct": 0.5, "max_adjust_pct": 15},
        }
        # base 50 + 10 (hvac) + 5 (humidity 70-60=10, 10*0.5=5) = 65
        speed, contribs = apply_modifiers(50.0, mods, True, False, 70, True)
        assert speed == pytest.approx(65.0)
        assert len(contribs) == 2

    def test_disabled_modifier_skipped(self):
        mods = {"hvac_cooling_active": {"enabled": False, "speed_adjust_pct": 50}}
        speed, contribs = apply_modifiers(50.0, mods, True, False, None, True)
        assert speed == 50.0
        assert contribs == []

    def test_humidity_none_skips_modifier(self):
        """When humidity is None, the humidity modifier should be skipped without crashing."""
        mods = {"humidity": {"enabled": True, "threshold": 60, "speed_adjust_per_unit_pct": 0.5, "max_adjust_pct": 15}}
        speed, contribs = apply_modifiers(50.0, mods, False, False, None, True)
        assert speed == 50.0
        assert contribs == []


class TestIsNighttime:
    """Tests for _is_nighttime with midnight-crossing and non-crossing ranges."""

    def _mock_time(self, hour, minute=0):
        """Create a mock datetime that returns a specific time."""
        return datetime(2026, 1, 15, hour, minute, 0)

    def test_midnight_crossing_at_23(self):
        """22:00-08:00 range: 23:00 should be nighttime."""
        with patch("auto_fan.speed_curve.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(23, 0)
            assert _is_nighttime(22, 8) is True

    def test_midnight_crossing_at_07(self):
        """22:00-08:00 range: 07:00 should be nighttime."""
        with patch("auto_fan.speed_curve.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(7, 0)
            assert _is_nighttime(22, 8) is True

    def test_midnight_crossing_at_09(self):
        """22:00-08:00 range: 09:00 should NOT be nighttime."""
        with patch("auto_fan.speed_curve.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(9, 0)
            assert _is_nighttime(22, 8) is False

    def test_midnight_crossing_at_21(self):
        """22:00-08:00 range: 21:00 should NOT be nighttime."""
        with patch("auto_fan.speed_curve.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(21, 0)
            assert _is_nighttime(22, 8) is False

    def test_edge_exactly_at_start_hour(self):
        """22:00-08:00 range: exactly 22:00 should be nighttime."""
        with patch("auto_fan.speed_curve.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(22, 0)
            assert _is_nighttime(22, 8) is True

    def test_edge_exactly_at_end_hour(self):
        """22:00-08:00 range: exactly 08:00 should NOT be nighttime (end is exclusive)."""
        with patch("auto_fan.speed_curve.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(8, 0)
            assert _is_nighttime(22, 8) is False

    def test_non_crossing_range_inside(self):
        """06:00-18:00 range (no midnight cross): 12:00 should be nighttime."""
        with patch("auto_fan.speed_curve.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(12, 0)
            assert _is_nighttime(6, 18) is True

    def test_non_crossing_range_outside(self):
        """06:00-18:00 range (no midnight cross): 20:00 should NOT be nighttime."""
        with patch("auto_fan.speed_curve.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(20, 0)
            assert _is_nighttime(6, 18) is False

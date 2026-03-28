"""Tests for speed curve interpolation and modifier stacking."""
import pytest
from unittest.mock import patch
from datetime import datetime, time
from auto_fan.speed_curve import interpolate, calculate_base_speed, apply_modifiers, _is_nighttime


class TestInterpolate:
    """Tests for the control point interpolation function."""

    def test_empty_points_returns_zero(self):
        assert interpolate(5.0, []) == 0.0

    def test_single_point_returns_its_value(self):
        pts = [{"offset": 0, "speed": 50}]
        assert interpolate(0, pts) == 50
        assert interpolate(-5, pts) == 50
        assert interpolate(10, pts) == 50

    def test_clamp_below_lowest(self):
        pts = [{"offset": 0, "speed": 10}, {"offset": 5, "speed": 80}]
        assert interpolate(-3, pts) == 10

    def test_clamp_above_highest(self):
        pts = [{"offset": 0, "speed": 10}, {"offset": 5, "speed": 80}]
        assert interpolate(10, pts) == 80

    def test_exact_point_value(self):
        pts = [{"offset": 0, "speed": 0}, {"offset": 5, "speed": 100}]
        assert interpolate(0, pts) == 0
        assert interpolate(5, pts) == 100

    def test_midpoint_interpolation(self):
        pts = [{"offset": 0, "speed": 0}, {"offset": 10, "speed": 100}]
        assert interpolate(5, pts) == pytest.approx(50.0)

    def test_quarter_interpolation(self):
        pts = [{"offset": 0, "speed": 0}, {"offset": 8, "speed": 100}]
        assert interpolate(2, pts) == pytest.approx(25.0)

    def test_multi_segment_interpolation(self):
        pts = [
            {"offset": 0, "speed": 0},
            {"offset": 3, "speed": 50},
            {"offset": 6, "speed": 85},
            {"offset": 8, "speed": 100},
        ]
        # Between 0 and 3
        assert interpolate(1.5, pts) == pytest.approx(25.0)
        # Between 3 and 6
        assert interpolate(4.5, pts) == pytest.approx(67.5)
        # Between 6 and 8
        assert interpolate(7, pts) == pytest.approx(92.5)

    def test_negative_offsets(self):
        pts = [
            {"offset": -4, "speed": 20},
            {"offset": -2, "speed": 10},
            {"offset": 0, "speed": 0},
        ]
        assert interpolate(-3, pts) == pytest.approx(15.0)
        assert interpolate(-1, pts) == pytest.approx(5.0)
        assert interpolate(-5, pts) == 20  # clamped below

    def test_unsorted_points_still_work(self):
        pts = [
            {"offset": 5, "speed": 100},
            {"offset": 0, "speed": 0},
        ]
        assert interpolate(2.5, pts) == pytest.approx(50.0)


class TestCalculateBaseSpeed:
    """Tests for unified fan curve base speed calculation."""

    def test_positive_delta_interpolation(self):
        fan_curve = {"points": [
            {"offset": -3, "speed": 0},
            {"offset": 0, "speed": 30},
            {"offset": 3, "speed": 100},
        ]}
        speed = calculate_base_speed(2.0, fan_curve)
        # Between offset 0 (30%) and offset 3 (100%): t=2/3, speed=30+46.67=76.67
        assert speed == pytest.approx(76.667, abs=0.1)

    def test_negative_delta_interpolation(self):
        fan_curve = {"points": [
            {"offset": -3, "speed": 0},
            {"offset": 0, "speed": 30},
            {"offset": 3, "speed": 100},
        ]}
        speed = calculate_base_speed(-1.5, fan_curve)
        # Between offset -3 (0%) and offset 0 (30%): t=1.5/3=0.5, speed=15
        assert speed == pytest.approx(15.0)

    def test_zero_delta(self):
        fan_curve = {"points": [
            {"offset": -2, "speed": 10},
            {"offset": 0, "speed": 40},
            {"offset": 2, "speed": 90},
        ]}
        speed = calculate_base_speed(0.0, fan_curve)
        assert speed == 40.0

    def test_beyond_range_clamps(self):
        fan_curve = {"points": [
            {"offset": -3, "speed": 5},
            {"offset": 3, "speed": 95},
        ]}
        assert calculate_base_speed(-10.0, fan_curve) == 5.0
        assert calculate_base_speed(10.0, fan_curve) == 95.0

    def test_empty_points_returns_zero(self):
        fan_curve = {"points": []}
        assert calculate_base_speed(2.0, fan_curve) == 0.0

    def test_simple_linear_curve(self):
        """Curve matching old cooling curve: 0-5 delta maps to 0-100%."""
        fan_curve = {"points": [
            {"offset": -5, "speed": 50},
            {"offset": 0, "speed": 0},
            {"offset": 5, "speed": 100},
        ]}
        # Positive delta
        assert calculate_base_speed(2.5, fan_curve) == pytest.approx(50.0)
        # Negative delta
        assert calculate_base_speed(-2.5, fan_curve) == pytest.approx(25.0)
        # At target
        assert calculate_base_speed(0.0, fan_curve) == 0.0


class TestApplyModifiers:
    """Tests for modifier stacking (dropdown-based, no enabled flags)."""

    def test_no_modifiers_returns_base(self):
        speed, contribs = apply_modifiers(50.0, {}, False, False, None, True)
        assert speed == 50.0
        assert contribs == []

    def test_hvac_cooling_boost(self):
        mods = {"hvac_cooling_active": {"speed_boost_pct": 20}}
        speed, contribs = apply_modifiers(50.0, mods, True, False, None, True)
        assert speed == 70.0
        assert len(contribs) == 1
        assert "❄️" in contribs[0][0]

    def test_hvac_cooling_not_active(self):
        mods = {"hvac_cooling_active": {"speed_boost_pct": 20}}
        speed, contribs = apply_modifiers(50.0, mods, False, False, None, True)
        assert speed == 50.0
        assert contribs == []

    def test_hvac_cooling_zero_boost_skipped(self):
        """speed_boost_pct=0 means no boost (implicitly disabled)."""
        mods = {"hvac_cooling_active": {"speed_boost_pct": 0}}
        speed, contribs = apply_modifiers(50.0, mods, True, False, None, True)
        assert speed == 50.0
        assert contribs == []

    def test_hvac_cooling_clamp_min(self):
        """Cooling modifier can enforce a minimum speed."""
        mods = {"hvac_cooling_active": {"speed_boost_pct": 0, "clamp_min_pct": 30}}
        speed, contribs = apply_modifiers(10.0, mods, True, False, None, True)
        assert speed == 30.0

    def test_hvac_heating_positive_boost(self):
        """Heating can boost speed (e.g., reverse mode to circulate warm air)."""
        mods = {"hvac_heating_active": {"speed_adjust_pct": 20, "clamp_min_pct": 0}}
        speed, contribs = apply_modifiers(50.0, mods, False, True, None, True)
        assert speed == 70.0

    def test_hvac_heating_negative_reduction(self):
        """Heating can reduce speed (negative adjustment)."""
        mods = {"hvac_heating_active": {"speed_adjust_pct": -20, "clamp_min_pct": 0}}
        speed, contribs = apply_modifiers(50.0, mods, False, True, None, True)
        assert speed == 30.0

    def test_hvac_heating_clamp_min(self):
        mods = {"hvac_heating_active": {"speed_adjust_pct": -80, "clamp_min_pct": 10}}
        speed, contribs = apply_modifiers(50.0, mods, False, True, None, True)
        assert speed == 10.0  # clamped to min, not -30

    def test_hvac_heating_zero_adjust_skipped(self):
        """speed_adjust_pct=0 means no change (implicitly disabled)."""
        mods = {"hvac_heating_active": {"speed_adjust_pct": 0, "clamp_min_pct": 0}}
        speed, contribs = apply_modifiers(50.0, mods, False, True, None, True)
        assert speed == 50.0
        assert contribs == []

    def test_humidity_above_threshold(self):
        """Flat boost when humidity exceeds threshold."""
        mods = {"humidity": {"threshold": 60, "speed_boost_pct": 10}}
        speed, contribs = apply_modifiers(50.0, mods, False, False, 75, True)
        assert speed == 60.0

    def test_humidity_below_threshold(self):
        mods = {"humidity": {"threshold": 60, "speed_boost_pct": 10}}
        speed, contribs = apply_modifiers(50.0, mods, False, False, 55, True)
        assert speed == 50.0

    def test_humidity_zero_boost_skipped(self):
        """speed_boost_pct=0 means no boost (implicitly disabled)."""
        mods = {"humidity": {"threshold": 60, "speed_boost_pct": 0}}
        speed, contribs = apply_modifiers(50.0, mods, False, False, 75, True)
        assert speed == 50.0
        assert contribs == []

    def test_away_caps_speed(self):
        mods = {"away": {"clamp_max_pct": 0}}
        speed, contribs = apply_modifiers(75.0, mods, False, False, None, False)
        assert speed == 0.0

    def test_away_not_triggered_when_home(self):
        mods = {"away": {"clamp_max_pct": 0}}
        speed, contribs = apply_modifiers(75.0, mods, False, False, None, True)
        assert speed == 75.0

    def test_away_effectively_disabled(self):
        """clamp_max_pct=100 means no cap (implicitly disabled)."""
        mods = {"away": {"clamp_max_pct": 100}}
        speed, contribs = apply_modifiers(75.0, mods, False, False, None, False)
        assert speed == 75.0
        assert contribs == []

    def test_final_clamp_to_zero(self):
        mods = {"hvac_heating_active": {"speed_adjust_pct": -100, "clamp_min_pct": 0}}
        speed, contribs = apply_modifiers(20.0, mods, False, True, None, True)
        assert speed == 0.0

    def test_final_clamp_to_hundred(self):
        mods = {"hvac_cooling_active": {"speed_boost_pct": 80}}
        speed, contribs = apply_modifiers(80.0, mods, True, False, None, True)
        assert speed == 100.0  # clamped, not 160

    def test_multiple_modifiers_stack(self):
        mods = {
            "hvac_cooling_active": {"speed_boost_pct": 10},
            "humidity": {"threshold": 60, "speed_boost_pct": 10},
        }
        # base 50 + 10 (hvac) + 10 (humidity flat boost) = 70
        speed, contribs = apply_modifiers(50.0, mods, True, False, 70, True)
        assert speed == pytest.approx(70.0)
        assert len(contribs) == 2

    def test_humidity_none_skips_modifier(self):
        """When humidity is None, the humidity modifier should be skipped without crashing."""
        mods = {"humidity": {"threshold": 60, "speed_boost_pct": 20}}
        speed, contribs = apply_modifiers(50.0, mods, False, False, None, True)
        assert speed == 50.0
        assert contribs == []

    def test_nighttime_effectively_disabled(self):
        """clamp_max_pct=100 and clamp_min_pct=0 means no clamping."""
        mods = {"nighttime": {"clamp_min_pct": 0, "clamp_max_pct": 100, "night_start_hour": 22, "night_end_hour": 8}}
        with patch("auto_fan.speed_curve.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 15, 23, 0, 0)
            speed, contribs = apply_modifiers(75.0, mods, False, False, None, True)
            assert speed == 75.0
            assert contribs == []

    def test_hvac_heating_clamp_min_without_adjustment(self):
        """clamp_min should work even when speed_adjust_pct=0."""
        mods = {"hvac_heating_active": {"speed_adjust_pct": 0, "clamp_min_pct": 30}}
        speed, contribs = apply_modifiers(10.0, mods, False, True, None, True)
        assert speed == 30.0
        assert len(contribs) == 1
        assert "min" in contribs[0][1]

    def test_both_hvac_modes_active(self):
        """Both cooling and heating active should stack their effects."""
        mods = {
            "hvac_cooling_active": {"speed_boost_pct": 10},
            "hvac_heating_active": {"speed_adjust_pct": 20, "clamp_min_pct": 0},
        }
        speed, contribs = apply_modifiers(50.0, mods, True, True, None, True)
        assert speed == 80.0  # 50 + 10 + 20
        assert len(contribs) == 2

    def test_nighttime_and_away_stacking(self):
        """Nighttime clamp applied before away cap."""
        mods = {
            "nighttime": {"clamp_min_pct": 10, "clamp_max_pct": 50, "night_start_hour": 22, "night_end_hour": 8},
            "away": {"clamp_max_pct": 20},
        }
        with patch("auto_fan.speed_curve.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 15, 23, 0, 0)
            # 75 → clamped to 50 (nighttime) → capped at 20 (no presence)
            speed, contribs = apply_modifiers(75.0, mods, False, False, None, False)
            assert speed == 20.0
            assert len(contribs) == 2

    def test_all_modifiers_active(self):
        """Full modifier stack with all 5 types active."""
        mods = {
            "hvac_cooling_active": {"speed_boost_pct": 10, "clamp_min_pct": 0},
            "hvac_heating_active": {"speed_adjust_pct": 10, "clamp_min_pct": 0},
            "humidity": {"threshold": 60, "speed_boost_pct": 10},
            "nighttime": {"clamp_min_pct": 0, "clamp_max_pct": 75, "night_start_hour": 22, "night_end_hour": 8},
            "away": {"clamp_max_pct": 60},
        }
        with patch("auto_fan.speed_curve.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 15, 23, 0, 0)
            # base 50 + 10 (cool) + 10 (heat) + 10 (humidity) = 80
            # → nighttime clamp [0-75] → 75
            # → no presence cap 60 → 60
            speed, contribs = apply_modifiers(50.0, mods, True, True, 75, False)
            assert speed == 60.0
            assert len(contribs) == 5  # all 5 contributed


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

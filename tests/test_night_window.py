"""Tests for the per-season Night Window (Phase 1)."""
from datetime import datetime
from unittest.mock import patch

import pytest

from test_auto_fan_agent import _make_agent, _setup_zone_devices


def _zone(fake_indigo):
    _setup_zone_devices(fake_indigo, temp_value=75.0)
    agent = _make_agent()
    return agent, agent.config.zones[0]


class TestNightWindowConfig:
    def test_defaults_when_config_has_none(self, fake_indigo):
        _, zone = _zone(fake_indigo)
        assert zone.get_night_window("fall") == (22, 8)

    def test_migration_moves_hours_out_of_modifier(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Z",
            "modifiers": {"nighttime": {
                "fall": {"clamp_min_pct": 20, "clamp_max_pct": 100, "night_start_hour": 20, "night_end_hour": 8},
                "summer": {"clamp_min_pct": 0, "clamp_max_pct": 100, "night_start_hour": 23, "night_end_hour": 6},
            }},
        }
        AutoFanConfig._migrate_zone(zone_d)
        assert zone_d["night_window"]["fall"] == {"start_hour": 20, "end_hour": 8}
        assert zone_d["night_window"]["summer"] == {"start_hour": 23, "end_hour": 6}
        # seasons the modifier never mentioned get the defaults
        assert zone_d["night_window"]["spring"] == {"start_hour": 22, "end_hour": 8}
        assert "night_start_hour" not in zone_d["modifiers"]["nighttime"]["fall"]
        # the clamp survives: the window is now always-on, the clamp is optional
        assert zone_d["modifiers"]["nighttime"]["fall"]["clamp_min_pct"] == 20

    def test_existing_window_wins_over_modifier_hours(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Z",
            "night_window": {"fall": {"start_hour": 21, "end_hour": 7}},
            "modifiers": {"nighttime": {"fall": {"clamp_min_pct": 0, "clamp_max_pct": 100,
                                                 "night_start_hour": 20, "night_end_hour": 8}}},
        }
        AutoFanConfig._migrate_zone(zone_d)
        assert zone_d["night_window"]["fall"] == {"start_hour": 21, "end_hour": 7}
        assert "night_end_hour" not in zone_d["modifiers"]["nighttime"]["fall"]

    def test_zone_loads_window_from_config(self, fake_indigo):
        from auto_fan.fan_zone import FanZone
        _, zone = _zone(fake_indigo)
        zone.from_config_dict({"name": "Z", "night_window": {"winter": {"start_hour": 19, "end_hour": 9}}})
        assert zone.get_night_window("winter") == (19, 9)
        assert zone.get_night_window("summer") == (22, 8)


class TestIsNight:
    @pytest.mark.parametrize("hour,expected", [(19, False), (20, True), (23, True), (0, True), (7, True), (8, False), (12, False)])
    def test_window_crossing_midnight(self, fake_indigo, hour, expected):
        _, zone = _zone(fake_indigo)
        zone.night_window["fall"] = {"start_hour": 20, "end_hour": 8}
        assert zone.is_night("fall", now=datetime(2026, 10, 3, hour, 30)) is expected

    @pytest.mark.parametrize("hour,expected", [(23, False), (0, True), (5, True), (6, False)])
    def test_window_within_one_day(self, fake_indigo, hour, expected):
        _, zone = _zone(fake_indigo)
        zone.night_window["fall"] = {"start_hour": 0, "end_hour": 6}
        assert zone.is_night("fall", now=datetime(2026, 10, 3, hour, 0)) is expected

    def test_equal_hours_means_never_night(self, fake_indigo):
        _, zone = _zone(fake_indigo)
        zone.night_window["fall"] = {"start_hour": 8, "end_hour": 8}
        assert zone.is_night("fall", now=datetime(2026, 10, 3, 8, 0)) is False

    def test_uses_clock_and_current_season(self, fake_indigo, fake_clock):
        _, zone = _zone(fake_indigo)
        zone.night_window["fall"] = {"start_hour": 20, "end_hour": 8}
        with patch("auto_fan.fan_zone.get_current_season", return_value="fall"):
            fake_clock.set(datetime(2026, 10, 3, 21, 0))
            assert zone.is_night() is True
            fake_clock.set(datetime(2026, 10, 4, 9, 0))
            assert zone.is_night() is False

    def test_next_night_end_and_start(self, fake_indigo):
        _, zone = _zone(fake_indigo)
        zone.night_window["fall"] = {"start_hour": 20, "end_hour": 8}
        with patch("auto_fan.fan_zone.get_current_season", return_value="fall"):
            # at 22:22 the window ends tomorrow 08:00 and the next start is tomorrow 20:00
            assert zone.next_night_end(datetime(2026, 10, 3, 22, 22)) == datetime(2026, 10, 4, 8, 0)
            assert zone.next_night_start(datetime(2026, 10, 3, 22, 22)) == datetime(2026, 10, 4, 20, 0)
            # at 14:00 the window ends tomorrow 08:00 and starts today 20:00
            assert zone.next_night_end(datetime(2026, 10, 3, 14, 0)) == datetime(2026, 10, 4, 8, 0)
            assert zone.next_night_start(datetime(2026, 10, 3, 14, 0)) == datetime(2026, 10, 3, 20, 0)


class TestNightClampUsesWindow:
    def test_clamp_applies_inside_window_regardless_of_modifier_hours(self, fake_indigo, fake_clock):
        """The clamp reads the zone's Night Window, not hours inside the modifier."""
        _, zone = _zone(fake_indigo)
        zone.night_window["fall"] = {"start_hour": 20, "end_hour": 8}
        zone.modifiers = {"nighttime": {"fall": {"clamp_min_pct": 0, "clamp_max_pct": 30}}}
        zone.seasonal_curves["fall"] = {"temperature_range": 3, "num_points": 3,
                                        "points": [{"offset": -3, "speed": 0}, {"offset": 0, "speed": 60}, {"offset": 3, "speed": 100}]}
        with patch("auto_fan.fan_zone.get_current_season", return_value="fall"):
            fake_clock.set(datetime(2026, 10, 3, 23, 0))  # night → clamp to 30
            plan = zone.calculate_target_speed()
            assert plan.target_speed_pct == 30
            fake_clock.set(datetime(2026, 10, 3, 14, 0))  # day → no clamp
            plan = zone.calculate_target_speed()
            assert plan.target_speed_pct > 30

    def test_apply_modifiers_explicit_is_night(self):
        from auto_fan.speed_curve import apply_modifiers
        mods = {"nighttime": {"fall": {"clamp_min_pct": 20, "clamp_max_pct": 100}}}
        speed, contribs = apply_modifiers(5, mods, False, False, None, True, season="fall", is_night=True)
        assert speed == 20 and any("Nighttime" in c[1] for c in contribs)
        speed, contribs = apply_modifiers(5, mods, False, False, None, True, season="fall", is_night=False)
        assert speed == 5 and contribs == []


class TestMigrationIsPersisted:
    def test_load_config_writes_migrated_file_once(self, fake_indigo, tmp_path):
        import json
        from auto_fan.auto_fan_config import AutoFanConfig
        path = tmp_path / "auto_fan_conf.json"
        path.write_text(json.dumps({"plugin_config": {}, "zones": [{
            "name": "Z", "temp_sensor_dev_ids": [1],
            "modifiers": {"nighttime": {"fall": {"clamp_min_pct": 20, "clamp_max_pct": 100,
                                                 "night_start_hour": 20, "night_end_hour": 8}}}}]}))
        AutoFanConfig(str(path))
        saved = json.loads(path.read_text())
        assert saved["zones"][0]["night_window"]["fall"] == {"start_hour": 20, "end_hour": 8}
        assert "night_start_hour" not in saved["zones"][0]["modifiers"]["nighttime"]["fall"]
        backups = list((tmp_path / "auto_backups").iterdir())
        assert len(backups) == 1
        # a second load changes nothing and takes no new backup
        AutoFanConfig(str(path))
        assert len(list((tmp_path / "auto_backups").iterdir())) == 1

"""Tests for AutoFanConfig loading and zone parsing."""
import json
import os
import tempfile

import pytest

from conftest import Device


class TestAutoFanConfig:
    """Tests for config loading from JSON."""

    def _make_config(self, data: dict):
        from auto_fan.auto_fan_config import AutoFanConfig

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(data, tmp)
        tmp.close()

        config = AutoFanConfig(tmp.name)
        os.unlink(tmp.name)
        return config

    def test_load_empty_config(self, fake_indigo):
        config = self._make_config({
            "plugin_config": {
                "enabled": True,
                "default_lock_duration": 60,
                "default_lock_extension_duration": 30,

            },
            "zones": [],
        })
        assert config.default_lock_duration == 60
        assert config.default_lock_extension_duration == 30
        assert len(config.zones) == 0

    def test_load_single_zone(self, fake_indigo):
        config = self._make_config({
            "plugin_config": {
                "enabled": True,
                "default_lock_duration": 45,
                "default_lock_extension_duration": 15,

            },
            "zones": [
                {
                    "name": "Living Room",
                    "fan_dev_id": 100,
                    "temp_sensor_dev_ids": [200],
                    "presence_dev_ids": [300],
                    "ideal_temp_value": 72,
                    "speed_curves": {
                        "cooling_curve": {
                            "breakpoints": [{"delta": 0, "speed_pct": 0}, {"delta": 5, "speed_pct": 100}]
                        },
                        "warming_curve": {
                            "breakpoints": [{"delta": 0, "speed_pct": 0}]
                        },
                    },
                    "modifiers": {},
                }
            ],
        })
        assert config.default_lock_duration == 45
        assert len(config.zones) == 1
        assert config.zones[0].name == "Living Room"
        assert config.zones[0].fan_dev_id == 100
        assert config.zones[0].zone_index == 0

    def test_multiple_zones_get_indices(self, fake_indigo):
        config = self._make_config({
            "plugin_config": {
                "enabled": True,
                "default_lock_duration": 60,
                "default_lock_extension_duration": 30,

            },
            "zones": [
                {"name": "Zone A", "fan_dev_id": 100, "temp_sensor_dev_ids": [200], "presence_dev_ids": [300]},
                {"name": "Zone B", "fan_dev_id": 101, "temp_sensor_dev_ids": [201], "presence_dev_ids": [301]},
                {"name": "Zone C", "fan_dev_id": 102, "temp_sensor_dev_ids": [202], "presence_dev_ids": [302]},
            ],
        })
        assert len(config.zones) == 3
        assert config.zones[0].zone_index == 0
        assert config.zones[1].zone_index == 1
        assert config.zones[2].zone_index == 2

    def test_default_fan_curve(self, fake_indigo):
        """Zones without explicit curves should use defaults."""
        config = self._make_config({
            "plugin_config": {
                "enabled": True,
                "default_lock_duration": 60,
                "default_lock_extension_duration": 30,

            },
            "zones": [
                {"name": "Default Zone", "fan_dev_id": 100, "temp_sensor_dev_ids": [200], "presence_dev_ids": [300]},
            ],
        })
        zone = config.zones[0]
        # Should have default fan curve points
        points = zone.fan_curve.get("points", [])
        assert len(points) > 0

    def test_weather_dev_id(self, fake_indigo):
        config = self._make_config({
            "plugin_config": {
                "enabled": True,
                "default_lock_duration": 60,
                "default_lock_extension_duration": 30,

                "weather_dev_id": 999,
            },
            "zones": [],
        })
        assert config.weather_dev_id == 999

    def test_load_zone_with_new_fields(self, fake_indigo):
        """Verify new schema fields load correctly."""
        config = self._make_config({
            "plugin_config": {
                "enabled": True,
                "default_lock_duration": 60,
                "default_lock_extension_duration": 30,
            },
            "zones": [
                {
                    "name": "New Schema Zone",
                    "fan_dev_id": 100,
                    "temp_sensor_dev_ids": [200],
                    "presence_dev_ids": [300],
                    "humidity_dev_ids": [400, 401],
                    "ideal_temp_source": "thermostat",
                    "ideal_temp_var_id": 500,
                    "ideal_temp_value": 74,
                    "thermostat_dev_id": 600,
                }
            ],
        })
        zone = config.zones[0]
        assert zone.humidity_dev_ids == [400, 401]
        assert zone.ideal_temp_source == "thermostat"
        assert zone.ideal_temp_var_id == 500
        assert zone.ideal_temp_value == 74
        assert zone.thermostat_dev_id == 600

    def test_migration_preserves_new_fields_over_old(self, fake_indigo):
        """When both old and new fields exist, new fields win."""
        config = self._make_config({
            "plugin_config": {
                "enabled": True,
                "default_lock_duration": 60,
                "default_lock_extension_duration": 30,
            },
            "zones": [
                {
                    "name": "Mixed Fields",
                    "fan_dev_id": 100,
                    "temp_sensor_dev_ids": [200],
                    "presence_dev_ids": [300],
                    "humidity_dev_id": 400,
                    "humidity_dev_ids": [401, 402],
                    "ideal_temp_use_variable": True,
                    "ideal_temp_source": "thermostat",
                    "weather_dev_id_override": 500,
                }
            ],
        })
        zone = config.zones[0]
        # New fields should be preserved, old fields discarded
        assert zone.humidity_dev_ids == [401, 402]
        assert zone.ideal_temp_source == "thermostat"

    def test_migrate_speed_curves_to_seasonal_curves(self, fake_indigo):
        """Legacy speed_curves should be converted through chain to seasonal_curves."""
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "speed_curves": {
                "cooling_curve": {"breakpoints": [
                    {"delta": 0, "speed_pct": 0},
                    {"delta": 5, "speed_pct": 100},
                ]},
                "warming_curve": {"breakpoints": [
                    {"delta": 0, "speed_pct": 0},
                    {"delta": -5, "speed_pct": 50},
                ]},
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        assert "speed_curves" not in zone_d
        assert "fan_curve" not in zone_d
        assert "seasonal_curves" in zone_d
        for season in ("spring", "summer", "fall", "winter"):
            assert "points" in zone_d["seasonal_curves"][season]
            assert zone_d["seasonal_curves"][season]["temperature_range"] >= 1
            assert len(zone_d["seasonal_curves"][season]["points"]) == 7

    def test_migrate_speed_curves_preserves_existing_fan_curve(self, fake_indigo):
        """If fan_curve already exists, speed_curves should just be removed, then fan_curve migrates."""
        from auto_fan.auto_fan_config import AutoFanConfig
        fan_curve = {
            "temperature_range": 3,
            "num_points": 5,
            "points": [
                {"offset": -3, "speed": 0},
                {"offset": -1.5, "speed": 15},
                {"offset": 0, "speed": 30},
                {"offset": 1.5, "speed": 65},
                {"offset": 3, "speed": 100},
            ],
        }
        zone_d = {
            "name": "Test",
            "speed_curves": {"cooling_curve": {"breakpoints": []}},
            "fan_curve": fan_curve,
        }
        AutoFanConfig._migrate_zone(zone_d)
        assert "speed_curves" not in zone_d
        assert "fan_curve" not in zone_d
        assert "seasonal_curves" in zone_d
        for season in ("spring", "summer", "fall", "winter"):
            assert zone_d["seasonal_curves"][season]["temperature_range"] == 3
            assert len(zone_d["seasonal_curves"][season]["points"]) == 5

    def test_migrate_empty_speed_curves(self, fake_indigo):
        """Empty speed_curves should produce default seasonal_curves."""
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "speed_curves": {},
        }
        AutoFanConfig._migrate_zone(zone_d)
        assert "speed_curves" not in zone_d
        assert "fan_curve" not in zone_d
        assert "seasonal_curves" in zone_d
        for season in ("spring", "summer", "fall", "winter"):
            assert len(zone_d["seasonal_curves"][season]["points"]) == 7

    def test_migrate_fan_curve_to_seasonal_curves(self, fake_indigo):
        """Single fan_curve should be replicated to all 4 seasons."""
        from auto_fan.auto_fan_config import AutoFanConfig
        fan_curve = {
            "temperature_range": 3,
            "num_points": 5,
            "points": [
                {"offset": -3, "speed": 0}, {"offset": -1.5, "speed": 15},
                {"offset": 0, "speed": 30}, {"offset": 1.5, "speed": 65},
                {"offset": 3, "speed": 100},
            ],
        }
        zone_d = {"name": "Test", "fan_curve": fan_curve}
        AutoFanConfig._migrate_zone(zone_d)
        assert "fan_curve" not in zone_d
        assert "seasonal_curves" in zone_d
        for season in ("spring", "summer", "fall", "winter"):
            assert season in zone_d["seasonal_curves"]
            assert len(zone_d["seasonal_curves"][season]["points"]) == 5

    def test_migrate_fan_curve_preserves_existing_seasonal_curves(self, fake_indigo):
        """When seasonal_curves exists, fan_curve should just be removed."""
        from auto_fan.auto_fan_config import AutoFanConfig
        seasonal = {s: {"temperature_range": 3, "num_points": 7, "points": []}
                    for s in ("spring", "summer", "fall", "winter")}
        zone_d = {"name": "Test", "fan_curve": {"points": []}, "seasonal_curves": seasonal}
        AutoFanConfig._migrate_zone(zone_d)
        assert "fan_curve" not in zone_d
        assert zone_d["seasonal_curves"] is seasonal

    def test_full_migration_chain_speed_curves_to_seasonal(self, fake_indigo):
        """speed_curves -> fan_curve -> seasonal_curves in one migration pass."""
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "speed_curves": {
                "cooling_curve": {"breakpoints": [
                    {"delta": 0, "speed_pct": 0}, {"delta": 5, "speed_pct": 100},
                ]},
                "warming_curve": {"breakpoints": [
                    {"delta": 0, "speed_pct": 0}, {"delta": -5, "speed_pct": 50},
                ]},
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        assert "speed_curves" not in zone_d
        assert "fan_curve" not in zone_d
        assert "seasonal_curves" in zone_d
        for season in ("spring", "summer", "fall", "winter"):
            assert len(zone_d["seasonal_curves"][season]["points"]) == 7


class TestMigrateModifiers:
    """Tests for old-format modifier migration to dropdown-based integers."""

    def test_migrate_hvac_cooling_enabled(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "hvac_cooling_active": {"enabled": True, "speed_adjust_pct": 15}
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        cool = zone_d["modifiers"]["hvac_cooling_active"]
        assert "enabled" not in cool
        assert "speed_adjust_pct" not in cool
        assert cool["speed_boost_pct"] == 20  # 15 rounded to nearest 10
        assert cool["clamp_min_pct"] == 0

    def test_migrate_hvac_cooling_disabled(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "hvac_cooling_active": {"enabled": False, "speed_adjust_pct": 50}
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        cool = zone_d["modifiers"]["hvac_cooling_active"]
        assert "enabled" not in cool
        assert cool["speed_boost_pct"] == 0  # was disabled

    def test_migrate_hvac_heating_enabled(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "hvac_heating_active": {"enabled": True, "speed_adjust_pct": -20, "clamp_min_pct": 5}
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        heat = zone_d["modifiers"]["hvac_heating_active"]
        assert "enabled" not in heat
        assert heat["speed_adjust_pct"] == -20  # -20 rounds to -20
        assert heat["clamp_min_pct"] == 10  # 5 rounded to nearest 10

    def test_migrate_hvac_heating_disabled(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "hvac_heating_active": {"enabled": False, "speed_adjust_pct": -30}
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        heat = zone_d["modifiers"]["hvac_heating_active"]
        assert "enabled" not in heat
        assert heat["speed_adjust_pct"] == 0  # was disabled
        assert heat["clamp_min_pct"] == 0

    def test_migrate_nighttime_enabled(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "nighttime": {
                    "enabled": True, "clamp_min_pct": 0, "clamp_max_pct": 50,
                    "night_start_hour": 22, "night_end_hour": 8,
                }
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        night = zone_d["modifiers"]["nighttime"]
        # Should be per-season now
        for season in ("spring", "summer", "fall", "winter"):
            assert season in night
            assert night[season]["clamp_max_pct"] == 50
            assert night[season]["clamp_min_pct"] == 0
            assert night[season]["night_start_hour"] == 22
            assert night[season]["night_end_hour"] == 8

    def test_migrate_nighttime_disabled(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "nighttime": {
                    "enabled": False, "clamp_min_pct": 10, "clamp_max_pct": 40,
                    "night_start_hour": 22, "night_end_hour": 8,
                }
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        night = zone_d["modifiers"]["nighttime"]
        # Disabled → neutral values, then wrapped per-season
        for season in ("spring", "summer", "fall", "winter"):
            assert season in night
            assert night[season]["clamp_max_pct"] == 100  # effectively disabled
            assert night[season]["clamp_min_pct"] == 0

    def test_migrate_nighttime_flat_to_per_season(self, fake_indigo):
        """Flat nighttime (no enabled key) gets migrated to per-season."""
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "nighttime": {
                    "clamp_min_pct": 10, "clamp_max_pct": 60,
                    "night_start_hour": 21, "night_end_hour": 7,
                }
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        night = zone_d["modifiers"]["nighttime"]
        for season in ("spring", "summer", "fall", "winter"):
            assert season in night
            assert night[season]["clamp_min_pct"] == 10
            assert night[season]["clamp_max_pct"] == 60
            assert night[season]["night_start_hour"] == 21
            assert night[season]["night_end_hour"] == 7

    def test_migrate_nighttime_already_per_season(self, fake_indigo):
        """Per-season nighttime passes through unchanged."""
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "nighttime": {
                    "spring": {"clamp_min_pct": 0, "clamp_max_pct": 50, "night_start_hour": 21, "night_end_hour": 7},
                    "summer": {"clamp_min_pct": 0, "clamp_max_pct": 70, "night_start_hour": 22, "night_end_hour": 8},
                    "fall":   {"clamp_min_pct": 0, "clamp_max_pct": 50, "night_start_hour": 21, "night_end_hour": 7},
                    "winter": {"clamp_min_pct": 0, "clamp_max_pct": 40, "night_start_hour": 20, "night_end_hour": 7},
                }
            },
        }
        import copy
        expected = copy.deepcopy(zone_d["modifiers"]["nighttime"])
        AutoFanConfig._migrate_zone(zone_d)
        assert zone_d["modifiers"]["nighttime"] == expected

    def test_migrate_humidity_enabled(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "humidity": {
                    "enabled": True, "threshold": 60,
                    "speed_adjust_per_unit_pct": 0.5, "max_adjust_pct": 15,
                }
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        hum = zone_d["modifiers"]["humidity"]
        assert "enabled" not in hum
        assert "speed_adjust_per_unit_pct" not in hum
        assert "max_adjust_pct" not in hum
        assert hum["speed_boost_pct"] == 20  # 15 rounded to nearest 10
        assert hum["threshold"] == 60

    def test_migrate_humidity_disabled(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "humidity": {
                    "enabled": False, "threshold": 55,
                    "speed_adjust_per_unit_pct": 0.5, "max_adjust_pct": 15,
                }
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        hum = zone_d["modifiers"]["humidity"]
        assert "enabled" not in hum
        assert hum["speed_boost_pct"] == 0  # was disabled
        assert hum["threshold"] == 55  # 55 rounds to 55

    def test_migrate_no_presence_enabled(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "no_presence": {"enabled": True, "clamp_max_pct": 0}
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        no_pres = zone_d["modifiers"]["no_presence"]
        assert "enabled" not in no_pres
        assert no_pres["clamp_max_pct"] == 0

    def test_migrate_no_presence_disabled(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "no_presence": {"enabled": False, "clamp_max_pct": 0}
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        no_pres = zone_d["modifiers"]["no_presence"]
        assert "enabled" not in no_pres
        assert no_pres["clamp_max_pct"] == 100  # effectively disabled

    def test_no_migration_when_already_new_format(self, fake_indigo):
        """New-format modifiers (no 'enabled' key) should pass through unchanged."""
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "hvac_cooling_active": {"speed_boost_pct": 20, "clamp_min_pct": 0},
                "humidity": {"speed_boost_pct": 10, "threshold": 60},
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        assert zone_d["modifiers"]["hvac_cooling_active"]["speed_boost_pct"] == 20
        assert zone_d["modifiers"]["humidity"]["speed_boost_pct"] == 10

    def test_no_migration_when_no_modifiers(self, fake_indigo):
        """Zones without modifiers should not crash."""
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {"name": "Test"}
        AutoFanConfig._migrate_zone(zone_d)
        assert "modifiers" not in zone_d

    def test_migrate_all_modifiers_together(self, fake_indigo):
        """Full zone with all 5 old-format modifiers migrated in one pass."""
        import copy
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Full Zone",
            "modifiers": {
                "hvac_cooling_active": {"enabled": True, "speed_adjust_pct": 15},
                "hvac_heating_active": {"enabled": True, "speed_adjust_pct": -20, "clamp_min_pct": 5},
                "nighttime": {
                    "enabled": True, "clamp_min_pct": 0, "clamp_max_pct": 50,
                    "night_start_hour": 22, "night_end_hour": 8,
                },
                "humidity": {
                    "enabled": True, "threshold": 60,
                    "speed_adjust_per_unit_pct": 0.5, "max_adjust_pct": 15,
                },
                "no_presence": {"enabled": True, "clamp_max_pct": 0},
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        mods = zone_d["modifiers"]

        # No 'enabled' keys remain (nighttime is now per-season, check sub-dicts)
        for key, mod in mods.items():
            if key == "nighttime":
                for season_mod in mod.values():
                    assert "enabled" not in season_mod
            else:
                assert "enabled" not in mod

        # Cooling
        assert mods["hvac_cooling_active"]["speed_boost_pct"] == 20
        assert mods["hvac_cooling_active"]["clamp_min_pct"] == 0

        # Heating
        assert mods["hvac_heating_active"]["speed_adjust_pct"] == -20
        assert mods["hvac_heating_active"]["clamp_min_pct"] == 10

        # Nighttime (per-season)
        for season in ("spring", "summer", "fall", "winter"):
            assert mods["nighttime"][season]["clamp_max_pct"] == 50
            assert mods["nighttime"][season]["night_start_hour"] == 22

        # Humidity
        assert mods["humidity"]["speed_boost_pct"] == 20
        assert "speed_adjust_per_unit_pct" not in mods["humidity"]

        # No presence
        assert mods["no_presence"]["clamp_max_pct"] == 0

    def test_migration_is_idempotent(self, fake_indigo):
        """Running migration twice produces identical results."""
        import copy
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "modifiers": {
                "hvac_cooling_active": {"enabled": True, "speed_adjust_pct": 25},
                "no_presence": {"enabled": False, "clamp_max_pct": 0},
                "nighttime": {
                    "enabled": True, "clamp_min_pct": 0, "clamp_max_pct": 50,
                    "night_start_hour": 22, "night_end_hour": 8,
                },
            },
        }
        AutoFanConfig._migrate_zone(zone_d)
        after_first = copy.deepcopy(zone_d)
        AutoFanConfig._migrate_zone(zone_d)
        assert zone_d == after_first

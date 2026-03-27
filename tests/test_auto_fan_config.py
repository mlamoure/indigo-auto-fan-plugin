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

    def test_default_speed_curves(self, fake_indigo):
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
        # Should have default cooling curve breakpoints
        cooling = zone.speed_curves.get("cooling_curve", {}).get("breakpoints", [])
        assert len(cooling) > 0

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

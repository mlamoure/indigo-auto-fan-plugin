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

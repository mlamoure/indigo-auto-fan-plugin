"""Tests for FanZone class."""
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch
from conftest import Device, Variable

import indigo


class TestFanZoneTemperature:
    """Tests for temperature reading and delta calculation."""

    def _make_zone(self):
        from auto_fan.auto_fan_config import AutoFanConfig
        import json, tempfile, os

        schema_src = os.path.join(
            os.path.dirname(__file__), os.pardir,
            "Auto Fan.indigoPlugin", "Contents", "Server Plugin",
            "config_web_editor", "config", "config_schema.json"
        )
        conf = {
            "plugin_config": {
                "enabled": True,
                "default_lock_duration": 60,
                "default_lock_extension_duration": 30,

            },
            "zones": [
                {
                    "name": "Test Zone",
                    "fan_dev_id": 100,
                    "temp_sensor_dev_ids": [200],
                    "presence_dev_ids": [300],
                    "ideal_temp_value": 72,
                    "speed_curves": {
                        "cooling_curve": {
                            "breakpoints": [
                                {"delta": 0, "speed_pct": 0},
                                {"delta": 5, "speed_pct": 100},
                            ]
                        },
                        "warming_curve": {
                            "breakpoints": [
                                {"delta": 0, "speed_pct": 0},
                                {"delta": -5, "speed_pct": 50},
                            ]
                        },
                    },
                    "modifiers": {},
                }
            ],
        }

        # Create temp config file
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(conf, tmp)
        tmp.close()

        config = AutoFanConfig(tmp.name)
        os.unlink(tmp.name)
        return config.zones[0], config

    def test_single_sensor_temperature(self, fake_indigo):
        # Set up temp sensor
        fake_indigo.devices[200] = Device(200, name="Temp Sensor", sensorValue=75.0)
        fake_indigo.devices[200].states["sensorValue"] = 75.0

        zone, config = self._make_zone()
        assert zone.get_current_temperature() == 75.0

    def test_multiple_sensor_average(self, fake_indigo):
        fake_indigo.devices[200] = Device(200, name="Temp 1", sensorValue=74.0)
        fake_indigo.devices[200].states["sensorValue"] = 74.0
        fake_indigo.devices[201] = Device(201, name="Temp 2", sensorValue=76.0)
        fake_indigo.devices[201].states["sensorValue"] = 76.0

        zone, config = self._make_zone()
        zone.temp_sensor_dev_ids = [200, 201]
        assert zone.get_current_temperature() == 75.0

    def test_temperature_delta_positive(self, fake_indigo):
        fake_indigo.devices[200] = Device(200, name="Temp", sensorValue=77.0)
        fake_indigo.devices[200].states["sensorValue"] = 77.0

        zone, config = self._make_zone()
        delta = zone.get_temperature_delta()
        assert delta == pytest.approx(5.0)  # 77 - 72

    def test_temperature_delta_negative(self, fake_indigo):
        fake_indigo.devices[200] = Device(200, name="Temp", sensorValue=69.0)
        fake_indigo.devices[200].states["sensorValue"] = 69.0

        zone, config = self._make_zone()
        delta = zone.get_temperature_delta()
        assert delta == pytest.approx(-3.0)  # 69 - 72

    def test_ideal_temp_from_variable(self, fake_indigo):
        fake_indigo.devices[200] = Device(200, name="Temp", sensorValue=75.0)
        fake_indigo.devices[200].states["sensorValue"] = 75.0
        fake_indigo.variables[500] = Variable(500, name="ideal_temp", value="70")

        zone, config = self._make_zone()
        # Set all seasons to variable source for simplicity
        for s in zone.seasonal_ideal_temp:
            zone.seasonal_ideal_temp[s] = {"source": "variable", "value": 72.0, "var_id": 500}

        assert zone.get_ideal_temperature() == 70.0
        assert zone.get_temperature_delta() == pytest.approx(5.0)

    def test_ideal_temp_from_thermostat_both_setpoints(self, fake_indigo):
        fake_indigo.devices[200] = Device(200, name="Temp", sensorValue=75.0)
        fake_indigo.devices[200].states["sensorValue"] = 75.0
        fake_indigo.devices[400] = Device(400, name="Thermostat", heatSetpoint=68.0, coolSetpoint=76.0)

        zone, config = self._make_zone()
        zone.thermostat_dev_id = 400
        for s in zone.seasonal_ideal_temp:
            zone.seasonal_ideal_temp[s] = {"source": "thermostat", "value": 72.0, "var_id": None}

        assert zone.get_ideal_temperature() == 72.0  # (68 + 76) / 2

    def test_ideal_temp_from_thermostat_heat_only(self, fake_indigo):
        fake_indigo.devices[200] = Device(200, name="Temp", sensorValue=75.0)
        fake_indigo.devices[200].states["sensorValue"] = 75.0
        fake_indigo.devices[400] = Device(400, name="Thermostat", heatSetpoint=70.0)

        zone, config = self._make_zone()
        zone.thermostat_dev_id = 400
        for s in zone.seasonal_ideal_temp:
            zone.seasonal_ideal_temp[s] = {"source": "thermostat", "value": 72.0, "var_id": None}

        assert zone.get_ideal_temperature() == 70.0

    def test_ideal_temp_from_thermostat_cool_only(self, fake_indigo):
        fake_indigo.devices[200] = Device(200, name="Temp", sensorValue=75.0)
        fake_indigo.devices[200].states["sensorValue"] = 75.0
        fake_indigo.devices[400] = Device(400, name="Thermostat", coolSetpoint=78.0)

        zone, config = self._make_zone()
        zone.thermostat_dev_id = 400
        for s in zone.seasonal_ideal_temp:
            zone.seasonal_ideal_temp[s] = {"source": "thermostat", "value": 72.0, "var_id": None}

        assert zone.get_ideal_temperature() == 78.0

    def test_ideal_temp_from_thermostat_no_setpoints_fallback(self, fake_indigo):
        fake_indigo.devices[200] = Device(200, name="Temp", sensorValue=75.0)
        fake_indigo.devices[200].states["sensorValue"] = 75.0

        zone, config = self._make_zone()
        for s in zone.seasonal_ideal_temp:
            zone.seasonal_ideal_temp[s] = {"source": "thermostat", "value": 72.0, "var_id": None}
        # No thermostat configured -> fallback to value
        assert zone.get_ideal_temperature() == 72.0

    def test_ideal_temp_variable_fallback_on_error(self, fake_indigo):
        """Variable lookup fails -> falls back to value."""
        fake_indigo.devices[200] = Device(200, name="Temp", sensorValue=75.0)
        fake_indigo.devices[200].states["sensorValue"] = 75.0

        zone, config = self._make_zone()
        for s in zone.seasonal_ideal_temp:
            zone.seasonal_ideal_temp[s] = {"source": "variable", "value": 72.0, "var_id": 999}

        assert zone.get_ideal_temperature() == 72.0  # falls back to value

    def test_ideal_temp_season_specific(self, fake_indigo):
        """Different ideal temps per season."""
        fake_indigo.devices[200] = Device(200, name="Temp", sensorValue=75.0)
        fake_indigo.devices[200].states["sensorValue"] = 75.0

        zone, config = self._make_zone()
        zone.seasonal_ideal_temp["summer"] = {"source": "static", "value": 70.0, "var_id": None}
        zone.seasonal_ideal_temp["winter"] = {"source": "static", "value": 74.0, "var_id": None}

        with patch("auto_fan.fan_zone.get_current_season", return_value="summer"):
            assert zone.get_ideal_temperature() == 70.0

        with patch("auto_fan.fan_zone.get_current_season", return_value="winter"):
            assert zone.get_ideal_temperature() == 74.0

    def test_no_sensors_returns_none(self, fake_indigo):
        zone, config = self._make_zone()
        zone.temp_sensor_dev_ids = []
        assert zone.get_current_temperature() is None
        assert zone.get_temperature_delta() is None


class TestFanZonePresence:
    """Tests for presence detection."""

    def _make_zone(self):
        """Reuse the same helper."""
        return TestFanZoneTemperature._make_zone(self)

    def test_presence_detected(self, fake_indigo):
        fake_indigo.devices[300] = Device(300, name="Motion", onState=True)

        zone, config = self._make_zone()
        assert zone.has_presence_detected() is True

    def test_no_presence(self, fake_indigo):
        fake_indigo.devices[300] = Device(300, name="Motion", onState=False)

        zone, config = self._make_zone()
        assert zone.has_presence_detected() is False

    def test_no_sensors_means_always_present(self, fake_indigo):
        zone, config = self._make_zone()
        zone.presence_dev_ids = []
        assert zone.has_presence_detected() is True


class TestFanZoneLocking:
    """Tests for zone lock management."""

    def _make_zone(self):
        return TestFanZoneTemperature._make_zone(self)

    def test_lock_zone(self, fake_indigo):
        zone, config = self._make_zone()
        zone.lock_zone("test")
        assert zone.locked is True
        assert zone.lock_expiration is not None
        assert zone.lock_expiration > datetime.now()

    def test_unlock_zone(self, fake_indigo):
        zone, config = self._make_zone()
        zone.lock_zone("test")
        zone.unlock_zone()
        assert zone.locked is False
        assert zone.lock_expiration is None

    def test_lock_expired(self, fake_indigo):
        zone, config = self._make_zone()
        zone.locked = True
        zone.lock_expiration = datetime.now() - timedelta(minutes=1)
        assert zone.is_lock_expired() is True

    def test_lock_not_expired(self, fake_indigo):
        zone, config = self._make_zone()
        zone.lock_zone("test")
        assert zone.is_lock_expired() is False

    def test_effective_lock_duration_uses_zone_override(self, fake_indigo):
        zone, config = self._make_zone()
        zone.lock_duration = 30
        assert zone.get_effective_lock_duration() == 30

    def test_effective_lock_duration_uses_default(self, fake_indigo):
        zone, config = self._make_zone()
        zone.lock_duration = -1
        assert zone.get_effective_lock_duration() == 60  # plugin default

    def test_self_triggered_change_within_grace(self, fake_indigo):
        zone, config = self._make_zone()
        zone._last_speed_command_time = datetime.now()
        assert zone.is_self_triggered_change() is True

    def test_self_triggered_change_after_grace(self, fake_indigo):
        zone, config = self._make_zone()
        zone._last_speed_command_time = datetime.now() - timedelta(seconds=15)
        assert zone.is_self_triggered_change() is False

    def test_self_triggered_change_no_command(self, fake_indigo):
        zone, config = self._make_zone()
        assert zone.is_self_triggered_change() is False


class TestFanZoneSpeedCalc:
    """Tests for calculate_target_speed."""

    def _make_zone(self):
        return TestFanZoneTemperature._make_zone(self)

    def test_basic_cooling_speed(self, fake_indigo):
        """Room 2.5 degrees above ideal -> 50% speed on fan curve."""
        fake_indigo.devices[200] = Device(200, name="Temp", sensorValue=74.5)
        fake_indigo.devices[200].states["sensorValue"] = 74.5
        fake_indigo.devices[300] = Device(300, name="Motion", onState=True)
        fake_indigo.devices[100] = Device(100, name="Fan", speedLevel=0)

        zone, config = self._make_zone()
        plan = zone.calculate_target_speed()

        # delta = 74.5 - 72 = 2.5 -> fan curve: between offset 0 (0%) and offset 5 (100%): 50%
        assert plan.target_speed_pct == pytest.approx(50.0)

    def test_warming_speed(self, fake_indigo):
        """Room below ideal -> interpolate on negative side of fan curve."""
        fake_indigo.devices[200] = Device(200, name="Temp", sensorValue=69.5)
        fake_indigo.devices[200].states["sensorValue"] = 69.5
        fake_indigo.devices[300] = Device(300, name="Motion", onState=True)
        fake_indigo.devices[100] = Device(100, name="Fan", speedLevel=0)

        zone, config = self._make_zone()
        plan = zone.calculate_target_speed()

        # delta = 69.5 - 72 = -2.5 -> fan curve: between offset -5 (50%) and offset 0 (0%): 25%
        assert plan.target_speed_pct == pytest.approx(25.0)

    def test_disabled_zone_returns_zero(self, fake_indigo):
        zone, config = self._make_zone()
        zone.enabled = False
        plan = zone.calculate_target_speed()
        assert plan.target_speed_pct == 0.0
        assert len(plan.exclusions) > 0

    def test_missing_temp_data(self, fake_indigo):
        fake_indigo.devices[300] = Device(300, name="Motion", onState=True)

        zone, config = self._make_zone()
        zone.temp_sensor_dev_ids = []
        plan = zone.calculate_target_speed()
        assert len(plan.exclusions) > 0

    def test_has_device(self, fake_indigo):
        zone, config = self._make_zone()
        assert zone.has_device(100) is True   # fan
        assert zone.has_device(200) is True   # temp sensor
        assert zone.has_device(300) is True   # presence
        assert zone.has_device(999) is False  # unknown

    def test_has_device_with_humidity_ids(self, fake_indigo):
        zone, config = self._make_zone()
        zone.humidity_dev_ids = [400, 401]
        assert zone.has_device(400) is True
        assert zone.has_device(401) is True
        assert zone.has_device(999) is False

    def test_has_variable_with_variable_source(self, fake_indigo):
        zone, config = self._make_zone()
        zone.seasonal_ideal_temp["summer"] = {"source": "variable", "value": 72.0, "var_id": 500}
        assert zone.has_variable(500) is True
        assert zone.has_variable(999) is False

    def test_has_variable_with_static_source(self, fake_indigo):
        zone, config = self._make_zone()
        # All seasons are static (default)
        assert zone.has_variable(500) is False

    def test_has_variable_checks_all_seasons(self, fake_indigo):
        """has_variable should match var_id from any season, not just current."""
        zone, config = self._make_zone()
        zone.seasonal_ideal_temp["winter"] = {"source": "variable", "value": 72.0, "var_id": 500}
        # Even if current season isn't winter, should still match
        assert zone.has_variable(500) is True

    def test_has_variable_with_thermostat_source(self, fake_indigo):
        zone, config = self._make_zone()
        for s in zone.seasonal_ideal_temp:
            zone.seasonal_ideal_temp[s] = {"source": "thermostat", "value": 72.0, "var_id": 500}
        assert zone.has_variable(500) is False

    def test_seasonal_curve_selection(self, fake_indigo):
        """Speed calculation should use the curve for the current season."""
        fake_indigo.devices[200] = Device(200, name="Temp", sensorValue=74.5)
        fake_indigo.devices[200].states["sensorValue"] = 74.5
        fake_indigo.devices[300] = Device(300, name="Motion", onState=True)
        fake_indigo.devices[100] = Device(100, name="Fan", speedLevel=0)

        zone, config = self._make_zone()
        # Set winter curve to produce very low speeds
        zone.seasonal_curves["winter"] = {
            "temperature_range": 3, "num_points": 3,
            "points": [
                {"offset": -3, "speed": 0},
                {"offset": 0, "speed": 0},
                {"offset": 3, "speed": 10},
            ]
        }

        # Mock to winter (January)
        with patch("auto_fan.fan_zone.get_current_season", return_value="winter"):
            plan = zone.calculate_target_speed()
            assert plan.target_speed_pct < 20

    def test_fan_curve_property_returns_current_season(self, fake_indigo):
        """fan_curve property should return the curve for the mocked season."""
        zone, config = self._make_zone()
        zone.seasonal_curves["summer"] = {
            "temperature_range": 5, "num_points": 3,
            "points": [{"offset": -5, "speed": 0}, {"offset": 0, "speed": 50}, {"offset": 5, "speed": 100}]
        }

        with patch("auto_fan.fan_zone.get_current_season", return_value="summer"):
            curve = zone.fan_curve
            assert curve["temperature_range"] == 5


class TestFanZoneHVAC:
    """Tests for HVAC-related methods with a thermostat device."""

    def _make_zone(self):
        return TestFanZoneTemperature._make_zone(self)

    def test_get_heat_setpoint(self, fake_indigo):
        fake_indigo.devices[400] = Device(400, name="Thermostat", heatSetpoint=68.0)
        zone, config = self._make_zone()
        zone.thermostat_dev_id = 400
        assert zone.get_heat_setpoint() == 68.0

    def test_get_heat_setpoint_no_thermostat(self, fake_indigo):
        zone, config = self._make_zone()
        assert zone.get_heat_setpoint() is None

    def test_get_cool_setpoint(self, fake_indigo):
        fake_indigo.devices[400] = Device(400, name="Thermostat", coolSetpoint=76.0)
        zone, config = self._make_zone()
        zone.thermostat_dev_id = 400
        assert zone.get_cool_setpoint() == 76.0

    def test_get_cool_setpoint_no_thermostat(self, fake_indigo):
        zone, config = self._make_zone()
        assert zone.get_cool_setpoint() is None

    def test_is_hvac_cooling(self, fake_indigo):
        dev = Device(400, name="Thermostat", hvacMode="cool")
        dev.states["hvacOperationMode"] = "cool"
        fake_indigo.devices[400] = dev
        zone, config = self._make_zone()
        zone.thermostat_dev_id = 400
        assert zone.is_hvac_cooling() is True
        assert zone.is_hvac_heating() is False

    def test_is_hvac_heating(self, fake_indigo):
        dev = Device(400, name="Thermostat", hvacMode="heat")
        dev.states["hvacOperationMode"] = "heat"
        fake_indigo.devices[400] = dev
        zone, config = self._make_zone()
        zone.thermostat_dev_id = 400
        assert zone.is_hvac_heating() is True
        assert zone.is_hvac_cooling() is False


class TestFanZoneExtendLock:
    """Tests for extend_lock method."""

    def _make_zone(self):
        return TestFanZoneTemperature._make_zone(self)

    def test_extend_lock(self, fake_indigo):
        zone, config = self._make_zone()
        zone.lock_zone("test")

        # Simulate lock almost expired by setting expiration to 1 minute from now
        zone.lock_expiration = datetime.now() + timedelta(minutes=1)
        old_expiration = zone.lock_expiration

        zone.extend_lock()
        assert zone.locked is True
        assert zone.lock_expiration is not None
        # Extension duration is 30 min, so new expiration should be well past the old 1-min one
        assert zone.lock_expiration > old_expiration

    def test_extend_lock_when_not_locked_is_noop(self, fake_indigo):
        zone, config = self._make_zone()
        zone.extend_lock()
        assert zone.locked is False
        assert zone.lock_expiration is None


class TestFanZoneHumidity:
    """Tests for get_humidity method."""

    def _make_zone(self):
        return TestFanZoneTemperature._make_zone(self)

    def test_get_humidity_with_sensor(self, fake_indigo):
        dev = Device(400, name="Humidity Sensor", sensorValue=65.0)
        dev.states["sensorValue"] = 65.0
        fake_indigo.devices[400] = dev
        zone, config = self._make_zone()
        zone.humidity_dev_ids = [400]
        assert zone.get_humidity() == 65.0

    def test_get_humidity_multiple_sensors_average(self, fake_indigo):
        dev1 = Device(400, name="Humidity 1", sensorValue=60.0)
        dev1.states["sensorValue"] = 60.0
        fake_indigo.devices[400] = dev1
        dev2 = Device(401, name="Humidity 2", sensorValue=70.0)
        dev2.states["sensorValue"] = 70.0
        fake_indigo.devices[401] = dev2
        zone, config = self._make_zone()
        zone.humidity_dev_ids = [400, 401]
        assert zone.get_humidity() == 65.0

    def test_get_humidity_no_sensor(self, fake_indigo):
        zone, config = self._make_zone()
        assert zone.get_humidity() is None


class TestFanZoneOutdoorTemperature:
    """Tests for get_outdoor_temperature method."""

    def _make_zone(self):
        return TestFanZoneTemperature._make_zone(self)

    def test_global_config_weather_device(self, fake_indigo):
        dev = Device(600, name="Global Weather")
        dev.states["temp"] = 90.0
        fake_indigo.devices[600] = dev
        zone, config = self._make_zone()
        config.weather_dev_id = 600
        assert zone.get_outdoor_temperature() == 90.0

    def test_no_weather_device(self, fake_indigo):
        zone, config = self._make_zone()
        assert zone.get_outdoor_temperature() is None


class TestConfigMigration:
    """Tests for legacy config migration."""

    def test_migrate_humidity_dev_id_to_ids(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {"name": "Test", "humidity_dev_id": 400}
        AutoFanConfig._migrate_zone(zone_d)
        assert zone_d.get("humidity_dev_ids") == [400]
        assert "humidity_dev_id" not in zone_d

    def test_migrate_humidity_dev_id_none(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {"name": "Test", "humidity_dev_id": None}
        AutoFanConfig._migrate_zone(zone_d)
        assert zone_d.get("humidity_dev_ids") == []
        assert "humidity_dev_id" not in zone_d

    def test_migrate_ideal_temp_use_variable_true(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {"name": "Test", "ideal_temp_use_variable": True, "ideal_temp_var_id": 500}
        AutoFanConfig._migrate_zone(zone_d)
        # Should end up in seasonal_ideal_temp with variable source
        assert "seasonal_ideal_temp" in zone_d
        for s in ("spring", "summer", "fall", "winter"):
            assert zone_d["seasonal_ideal_temp"][s]["source"] == "variable"
            assert zone_d["seasonal_ideal_temp"][s]["var_id"] == 500
        assert "ideal_temp_use_variable" not in zone_d
        assert "ideal_temp_source" not in zone_d

    def test_migrate_ideal_temp_use_variable_false(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {"name": "Test", "ideal_temp_use_variable": False, "ideal_temp_value": 74.0}
        AutoFanConfig._migrate_zone(zone_d)
        assert "seasonal_ideal_temp" in zone_d
        for s in ("spring", "summer", "fall", "winter"):
            assert zone_d["seasonal_ideal_temp"][s]["source"] == "static"
            assert zone_d["seasonal_ideal_temp"][s]["value"] == 74.0
        assert "ideal_temp_use_variable" not in zone_d

    def test_migrate_flat_ideal_temp_to_seasonal(self, fake_indigo):
        """Flat ideal_temp fields migrate to seasonal_ideal_temp."""
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {
            "name": "Test",
            "ideal_temp_source": "thermostat",
            "ideal_temp_value": 73.0,
            "ideal_temp_var_id": 500,
        }
        AutoFanConfig._migrate_zone(zone_d)
        assert "seasonal_ideal_temp" in zone_d
        assert "ideal_temp_source" not in zone_d
        assert "ideal_temp_value" not in zone_d
        assert "ideal_temp_var_id" not in zone_d
        for s in ("spring", "summer", "fall", "winter"):
            assert zone_d["seasonal_ideal_temp"][s]["source"] == "thermostat"
            assert zone_d["seasonal_ideal_temp"][s]["value"] == 73.0
            assert zone_d["seasonal_ideal_temp"][s]["var_id"] == 500

    def test_migrate_preserves_existing_seasonal_ideal_temp(self, fake_indigo):
        """When seasonal_ideal_temp exists, old flat fields are cleaned up."""
        from auto_fan.auto_fan_config import AutoFanConfig
        existing = {s: {"source": "static", "value": 70.0, "var_id": None} for s in ("spring", "summer", "fall", "winter")}
        zone_d = {
            "name": "Test",
            "seasonal_ideal_temp": existing,
            "ideal_temp_source": "static",
            "ideal_temp_value": 72.0,
        }
        AutoFanConfig._migrate_zone(zone_d)
        assert zone_d["seasonal_ideal_temp"] is existing
        assert "ideal_temp_source" not in zone_d
        assert "ideal_temp_value" not in zone_d

    def test_migrate_removes_weather_override(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        zone_d = {"name": "Test", "weather_dev_id_override": 500}
        AutoFanConfig._migrate_zone(zone_d)
        assert "weather_dev_id_override" not in zone_d

    def test_migrate_no_op_for_current_schema(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        seasonal = {s: {"source": "static", "value": 72.0, "var_id": None} for s in ("spring", "summer", "fall", "winter")}
        zone_d = {"name": "Test", "humidity_dev_ids": [400], "seasonal_ideal_temp": seasonal}
        AutoFanConfig._migrate_zone(zone_d)
        assert zone_d["humidity_dev_ids"] == [400]
        assert zone_d["seasonal_ideal_temp"] is seasonal


class TestGetSensorReadings:
    """Tests for get_sensor_readings method."""

    def _make_zone(self):
        return TestFanZoneTemperature._make_zone(self)

    def test_single_sensor(self, fake_indigo):
        fake_indigo.devices[200] = Device(200, name="Temp Sensor", sensorValue=75.0)
        fake_indigo.devices[200].states["sensorValue"] = 75.0

        zone, config = self._make_zone()
        readings = zone.get_sensor_readings()
        assert len(readings) == 1
        assert readings[0] == (200, "Temp Sensor", 75.0)

    def test_multiple_sensors(self, fake_indigo):
        fake_indigo.devices[200] = Device(200, name="Temp 1", sensorValue=74.0)
        fake_indigo.devices[200].states["sensorValue"] = 74.0
        fake_indigo.devices[201] = Device(201, name="Temp 2", sensorValue=76.0)
        fake_indigo.devices[201].states["sensorValue"] = 76.0

        zone, config = self._make_zone()
        zone.temp_sensor_dev_ids = [200, 201]
        readings = zone.get_sensor_readings()
        assert len(readings) == 2
        assert readings[0] == (200, "Temp 1", 74.0)
        assert readings[1] == (201, "Temp 2", 76.0)

    def test_no_sensors(self, fake_indigo):
        zone, config = self._make_zone()
        zone.temp_sensor_dev_ids = []
        assert zone.get_sensor_readings() == []

    def test_sensor_with_no_matching_state_key(self, fake_indigo):
        dev = Device(200, name="Odd Sensor")
        dev.states = {"unrelated_key": 42}
        fake_indigo.devices[200] = dev

        zone, config = self._make_zone()
        readings = zone.get_sensor_readings()
        assert len(readings) == 1
        assert readings[0] == (200, "Odd Sensor", None)


class TestIdealTemperatureBreakdown:
    """Tests for get_ideal_temperature_breakdown method."""

    def _make_zone(self):
        return TestFanZoneTemperature._make_zone(self)

    def test_static_source(self, fake_indigo):
        zone, config = self._make_zone()
        lines = zone.get_ideal_temperature_breakdown()
        # First line is season indicator, second is source
        has_static = any("static" in msg.lower() for _, msg in lines)
        has_value = any("72.0" in msg for _, msg in lines)
        assert has_static
        assert has_value

    def test_variable_source(self, fake_indigo):
        fake_indigo.variables[500] = Variable(500, name="ideal_temp", value="70")

        zone, config = self._make_zone()
        for s in zone.seasonal_ideal_temp:
            zone.seasonal_ideal_temp[s] = {"source": "variable", "value": 72.0, "var_id": 500}

        lines = zone.get_ideal_temperature_breakdown()
        has_var = any("ideal_temp" in msg for _, msg in lines)
        has_value = any("70.0" in msg for _, msg in lines)
        assert has_var
        assert has_value

    def test_variable_source_fallback(self, fake_indigo):
        zone, config = self._make_zone()
        for s in zone.seasonal_ideal_temp:
            zone.seasonal_ideal_temp[s] = {"source": "variable", "value": 72.0, "var_id": 999}

        lines = zone.get_ideal_temperature_breakdown()
        has_fallback = any("fallback" in msg.lower() for _, msg in lines)
        assert has_fallback

    def test_thermostat_both_setpoints(self, fake_indigo):
        fake_indigo.devices[400] = Device(400, name="Main HVAC", heatSetpoint=68.0, coolSetpoint=76.0)

        zone, config = self._make_zone()
        for s in zone.seasonal_ideal_temp:
            zone.seasonal_ideal_temp[s] = {"source": "thermostat", "value": 72.0, "var_id": None}
        zone.thermostat_dev_id = 400

        lines = zone.get_ideal_temperature_breakdown()
        all_text = " ".join(msg for _, msg in lines)
        assert "Main HVAC" in all_text
        assert "68.0" in all_text
        assert "76.0" in all_text
        assert "72.0" in all_text

    def test_thermostat_heat_only(self, fake_indigo):
        fake_indigo.devices[400] = Device(400, name="Heater", heatSetpoint=70.0)

        zone, config = self._make_zone()
        for s in zone.seasonal_ideal_temp:
            zone.seasonal_ideal_temp[s] = {"source": "thermostat", "value": 72.0, "var_id": None}
        zone.thermostat_dev_id = 400

        lines = zone.get_ideal_temperature_breakdown()
        has_heat = any("heat setpoint only" in msg.lower() for _, msg in lines)
        assert has_heat

    def test_thermostat_no_setpoints_fallback(self, fake_indigo):
        zone, config = self._make_zone()
        for s in zone.seasonal_ideal_temp:
            zone.seasonal_ideal_temp[s] = {"source": "thermostat", "value": 72.0, "var_id": None}
        # No thermostat device configured

        lines = zone.get_ideal_temperature_breakdown()
        has_fallback = any("fallback" in msg.lower() for _, msg in lines)
        assert has_fallback

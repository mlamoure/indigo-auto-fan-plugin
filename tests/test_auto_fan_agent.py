"""Tests for AutoFanAgent class."""
import pytest
from datetime import datetime, timedelta
from conftest import Device, Variable

import indigo


def _make_agent():
    """Create an AutoFanAgent with a single zone config."""
    from auto_fan.auto_fan_config import AutoFanConfig
    import json, tempfile, os

    conf = {
        "plugin_config": {
            "default_lock_duration": 60,
            "default_lock_extension_duration": 30,
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
            },
            {
                "name": "Bedroom",
                "fan_dev_id": 101,
                "temp_sensor_dev_ids": [201],
                "presence_dev_ids": [301],
                "ideal_temp_value": 70,
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
            },
        ],
    }

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(conf, tmp)
    tmp.close()

    config = AutoFanConfig(tmp.name)
    os.unlink(tmp.name)

    from auto_fan.auto_fan_agent import AutoFanAgent
    agent = AutoFanAgent(config)
    return agent


def _setup_zone_devices(fake_indigo, fan_id=100, temp_id=200, presence_id=300,
                        temp_value=75.0, fan_speed=0, presence=True):
    """Set up standard devices for a zone."""
    fake_indigo.devices[fan_id] = Device(fan_id, name=f"Fan-{fan_id}", speedLevel=fan_speed)
    fake_indigo.devices[temp_id] = Device(temp_id, name=f"Temp-{temp_id}", sensorValue=temp_value)
    fake_indigo.devices[presence_id] = Device(presence_id, name=f"Motion-{presence_id}", onState=presence)


class TestProcessZone:
    """Tests for process_zone method."""

    def test_happy_path_speed_change(self, fake_indigo, speed_control_calls):
        """Zone with temp above ideal should apply speed change."""
        _setup_zone_devices(fake_indigo, temp_value=75.0, fan_speed=0)
        agent = _make_agent()
        zone = agent.config.zones[0]

        result = agent.process_zone(zone)
        assert result is True
        # delta = 75 - 72 = 3, cooling curve: 3/5 * 100 = 60%
        assert len(speed_control_calls) > 0
        assert speed_control_calls[-1]["value"] == 60

    def test_disabled_zone_skipped(self, fake_indigo):
        """Disabled zone should be skipped."""
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()
        zone = agent.config.zones[0]
        zone.enabled = False

        result = agent.process_zone(zone)
        assert result is False

    def test_locked_zone_skipped(self, fake_indigo):
        """Locked zone should be skipped."""
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()
        zone = agent.config.zones[0]
        zone.lock_zone("test")

        result = agent.process_zone(zone)
        assert result is False

    def test_expired_lock_unlocks_then_processes(self, fake_indigo, speed_control_calls):
        """Zone with expired lock should unlock and then process normally."""
        _setup_zone_devices(fake_indigo, temp_value=75.0, fan_speed=0)
        agent = _make_agent()
        zone = agent.config.zones[0]

        # Set up an expired lock
        zone.locked = True
        zone.lock_expiration = datetime.now() - timedelta(minutes=1)

        result = agent.process_zone(zone)
        assert result is True
        assert zone.locked is False
        assert len(speed_control_calls) > 0


class TestProcessDeviceChange:
    """Tests for process_device_change method."""

    def test_fan_device_change_triggers_lock(self, fake_indigo):
        """Changing a fan device should lock the zone."""
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()
        zone = agent.config.zones[0]

        orig_dev = fake_indigo.devices[100]
        diff = {"speedLevel": 50}

        processed = agent.process_device_change(orig_dev, diff)
        assert len(processed) == 1
        assert processed[0].name == "Living Room"
        assert zone.locked is True

    def test_sensor_change_triggers_zone_reprocess(self, fake_indigo, speed_control_calls):
        """Changing a sensor device should reprocess the zone."""
        _setup_zone_devices(fake_indigo, temp_value=75.0, fan_speed=0)
        agent = _make_agent()

        orig_dev = fake_indigo.devices[200]
        diff = {"sensorValue": 76.0}

        processed = agent.process_device_change(orig_dev, diff)
        assert len(processed) == 1
        assert processed[0].name == "Living Room"

    def test_unknown_device_ignored(self, fake_indigo):
        """Device not belonging to any zone should be ignored."""
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()

        unknown_dev = Device(999, name="Unknown")
        fake_indigo.devices[999] = unknown_dev
        diff = {"sensorValue": 50}

        processed = agent.process_device_change(unknown_dev, diff)
        assert len(processed) == 0

    def test_fan_change_on_disabled_zone_ignored(self, fake_indigo):
        """Fan change on a disabled zone should be ignored (no lock)."""
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()
        zone = agent.config.zones[0]
        zone.enabled = False

        orig_dev = fake_indigo.devices[100]
        diff = {"speedLevel": 50}

        processed = agent.process_device_change(orig_dev, diff)
        assert len(processed) == 0
        assert zone.locked is False


class TestProcessVariableChange:
    """Tests for process_variable_change method."""

    def test_zone_ideal_temp_var_triggers_that_zone_only(self, fake_indigo, speed_control_calls):
        """Change to a zone's ideal temp variable should only trigger that zone."""
        _setup_zone_devices(fake_indigo, temp_value=75.0, fan_speed=0)
        _setup_zone_devices(fake_indigo, fan_id=101, temp_id=201,
                           presence_id=301, temp_value=73.0, fan_speed=0)
        agent = _make_agent()

        # Set up zone 0 to use variable for ideal temp
        zone = agent.config.zones[0]
        zone.ideal_temp_source = "variable"
        zone.ideal_temp_var_id = 500
        fake_indigo.variables[500] = Variable(500, name="ideal_temp", value="70")

        orig_var = Variable(500, name="ideal_temp", value="72")
        new_var = Variable(500, name="ideal_temp", value="70")

        processed = agent.process_variable_change(orig_var, new_var)
        assert len(processed) == 1
        assert processed[0].name == "Living Room"

    def test_unrelated_variable_ignored(self, fake_indigo):
        """Change to an unrelated variable should not trigger any zone."""
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()

        orig_var = Variable(999, name="random_var", value="old")
        new_var = Variable(999, name="random_var", value="new")
        fake_indigo.variables[999] = new_var

        processed = agent.process_variable_change(orig_var, new_var)
        assert len(processed) == 0


class TestResetLocks:
    """Tests for reset_locks method."""

    def test_reset_single_zone_lock(self, fake_indigo):
        """Reset lock on a specific zone."""
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        _setup_zone_devices(fake_indigo, fan_id=101, temp_id=201,
                           presence_id=301, temp_value=73.0)
        agent = _make_agent()

        zone0 = agent.config.zones[0]
        zone1 = agent.config.zones[1]
        zone0.lock_zone("test")
        zone1.lock_zone("test")

        agent.reset_locks(zone_name="Living Room")
        assert zone0.locked is False
        assert zone1.locked is True

    def test_reset_all_zone_locks(self, fake_indigo):
        """Reset all zone locks."""
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        _setup_zone_devices(fake_indigo, fan_id=101, temp_id=201,
                           presence_id=301, temp_value=73.0)
        agent = _make_agent()

        zone0 = agent.config.zones[0]
        zone1 = agent.config.zones[1]
        zone0.lock_zone("test")
        zone1.lock_zone("test")

        agent.reset_locks()
        assert zone0.locked is False
        assert zone1.locked is False


class TestEnableDisableZone:
    """Tests for enable_zone and disable_zone methods."""

    def test_enable_zone(self, fake_indigo):
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()
        zone = agent.config.zones[0]
        zone.enabled = False

        agent.enable_zone("Living Room")
        assert zone.enabled is True

    def test_disable_zone(self, fake_indigo):
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()
        zone = agent.config.zones[0]

        agent.disable_zone("Living Room")
        assert zone.enabled is False

    def test_enable_nonexistent_zone_is_noop(self, fake_indigo):
        """Enabling a zone that doesn't exist should not raise."""
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()
        agent.enable_zone("Nonexistent Zone")  # should not raise

    def test_disable_nonexistent_zone_is_noop(self, fake_indigo):
        """Disabling a zone that doesn't exist should not raise."""
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()
        agent.disable_zone("Nonexistent Zone")  # should not raise

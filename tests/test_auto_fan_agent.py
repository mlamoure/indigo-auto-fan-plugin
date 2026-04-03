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
                        temp_value=75.0, fan_speed=0, presence=True,
                        speed_index_count=4):
    """Set up standard devices for a zone."""
    # Compute initial speedIndex from fan_speed for SpeedControl devices
    if speed_index_count and speed_index_count > 1:
        speed_index = round(fan_speed * (speed_index_count - 1) / 100.0)
        speed_level = round(speed_index * 100.0 / (speed_index_count - 1))
    else:
        speed_index = None
        speed_level = fan_speed
    fake_indigo.devices[fan_id] = Device(
        fan_id, name=f"Fan-{fan_id}", speedLevel=speed_level,
        speedIndex=speed_index, speedIndexCount=speed_index_count,
    )
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

    def test_sensor_change_queued_for_debounce(self, fake_indigo, speed_control_calls):
        """Changing a sensor device should queue for debounced processing (not immediate)."""
        _setup_zone_devices(fake_indigo, temp_value=75.0, fan_speed=0)
        agent = _make_agent()

        orig_dev = fake_indigo.devices[200]
        diff = {"sensorValue": 76.0}

        # Sensor changes are debounced — not returned in processed list
        processed = agent.process_device_change(orig_dev, diff)
        assert len(processed) == 0
        # But the change should be queued
        assert "Living Room" in agent._pending_zone_changes
        assert 200 in agent._pending_zone_changes["Living Room"]
        agent.shutdown()

    def test_debounced_zone_processes_after_timer(self, fake_indigo, speed_control_calls):
        """Debounced zone should process correctly when timer fires."""
        _setup_zone_devices(fake_indigo, temp_value=75.0, fan_speed=0)
        agent = _make_agent()
        zone = agent.config.zones[0]

        orig_dev = fake_indigo.devices[200]
        diff = {"sensorValue": 76.0}

        agent.process_device_change(orig_dev, diff)
        # Simulate timer firing by calling _process_debounced_zone directly
        agent._debounce_timers["Living Room"].cancel()
        agent._process_debounced_zone(zone)
        # Zone should have been processed — speed should have changed
        assert len(speed_control_calls) > 0

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

        # Set up zone 0 to use variable for ideal temp (all seasons)
        zone = agent.config.zones[0]
        for s in zone.seasonal_ideal_temp:
            zone.seasonal_ideal_temp[s] = {"source": "variable", "value": 72.0, "var_id": 500}
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


class TestDebouncedProcessing:
    """Tests for debounced zone change aggregation."""

    def test_multiple_devices_aggregate(self, fake_indigo, speed_control_calls):
        """Multiple device changes for one zone should aggregate into one processing."""
        _setup_zone_devices(fake_indigo, temp_value=75.0, fan_speed=0)
        agent = _make_agent()
        zone = agent.config.zones[0]

        # Queue changes from multiple devices
        temp_dev = fake_indigo.devices[200]
        presence_dev = fake_indigo.devices[300]

        agent.process_device_change(temp_dev, {"sensorValue": 76.0})
        agent.process_device_change(presence_dev, {"onState": True})

        # Both should be queued under the same zone
        assert len(agent._pending_zone_changes["Living Room"]) == 2
        assert 200 in agent._pending_zone_changes["Living Room"]
        assert 300 in agent._pending_zone_changes["Living Room"]

        # Process manually (simulating timer fire)
        agent._debounce_timers["Living Room"].cancel()
        agent._process_debounced_zone(zone)

        # Should have processed once — pending cleared
        assert "Living Room" not in agent._pending_zone_changes
        assert len(speed_control_calls) > 0

    def test_same_device_merges_keeps_original_old(self, fake_indigo):
        """Same device updating twice should keep the earliest old_value."""
        _setup_zone_devices(fake_indigo, temp_value=67.0, fan_speed=0)
        agent = _make_agent()

        temp_dev = fake_indigo.devices[200]

        # First change: 67.0 -> 66.5
        agent.process_device_change(temp_dev, {"sensorValue": 66.5})
        first_old = agent._pending_zone_changes["Living Room"][200]["old_value"]

        # Second change: 66.5 -> 66.0 (but old_value should stay as 67.0)
        temp_dev.states["sensorValue"] = 66.5
        agent.process_device_change(temp_dev, {"sensorValue": 66.0})

        record = agent._pending_zone_changes["Living Room"][200]
        assert record["old_value"] == first_old
        assert record["new_value"] == 66.0
        agent.shutdown()

    def test_fan_device_not_debounced(self, fake_indigo):
        """Fan device changes should be immediate, not debounced."""
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()

        fan_dev = fake_indigo.devices[100]
        diff = {"speedLevel": 50}

        processed = agent.process_device_change(fan_dev, diff)
        # Fan changes return immediately in processed list
        assert len(processed) == 1
        assert processed[0].name == "Living Room"
        # No debounce state for fan changes
        assert "Living Room" not in agent._pending_zone_changes

    def test_debounced_no_speed_change_no_log(self, fake_indigo, speed_control_calls):
        """Debounced processing with no speed change should not call apply_speed_change."""
        # Set fan to match what the calculation would produce
        _setup_zone_devices(fake_indigo, temp_value=75.0, fan_speed=60)
        agent = _make_agent()
        zone = agent.config.zones[0]

        temp_dev = fake_indigo.devices[200]
        agent.process_device_change(temp_dev, {"sensorValue": 75.0})

        agent._debounce_timers["Living Room"].cancel()
        agent._process_debounced_zone(zone)

        # No speed control calls since speed already matches
        assert len(speed_control_calls) == 0


class TestClassifyDeviceChange:
    """Tests for _classify_device_change."""

    def test_classify_temperature(self, fake_indigo):
        _setup_zone_devices(fake_indigo, temp_value=67.0)
        agent = _make_agent()
        zone = agent.config.zones[0]

        orig_dev = fake_indigo.devices[200]
        diff = {"sensorValue": 66.5}

        record = agent._classify_device_change(zone, orig_dev, diff)
        assert record["role"] == "temperature"
        assert record["old_value"] == 67.0
        assert record["new_value"] == 66.5

    def test_classify_humidity(self, fake_indigo):
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()
        zone = agent.config.zones[0]

        # Add humidity sensor to zone
        hum_dev = Device(400, name="Humidity Sensor", sensorValue=54.0)
        fake_indigo.devices[400] = hum_dev
        zone.humidity_dev_ids = [400]

        record = agent._classify_device_change(zone, hum_dev, {"sensorValue": 56.0})
        assert record["role"] == "humidity"
        assert record["old_value"] == 54.0
        assert record["new_value"] == 56.0

    def test_classify_presence(self, fake_indigo):
        _setup_zone_devices(fake_indigo, temp_value=75.0, presence=False)
        agent = _make_agent()
        zone = agent.config.zones[0]

        presence_dev = fake_indigo.devices[300]
        record = agent._classify_device_change(zone, presence_dev, {"onState": True})
        assert record["role"] == "presence"
        assert record["new_value"] is True

    def test_classify_weather(self, fake_indigo):
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()
        zone = agent.config.zones[0]

        weather_dev = Device(500, name="Weather Station", sensorValue=85.0)
        weather_dev.states["temp"] = 85.0
        fake_indigo.devices[500] = weather_dev
        agent.config.weather_dev_id = 500

        record = agent._classify_device_change(zone, weather_dev, {"temp": 87.0})
        assert record["role"] == "weather"
        assert record["old_value"] == 85.0
        assert record["new_value"] == 87.0

    def test_classify_unknown_device(self, fake_indigo):
        _setup_zone_devices(fake_indigo, temp_value=75.0)
        agent = _make_agent()
        zone = agent.config.zones[0]

        mystery_dev = Device(999, name="Mystery")
        record = agent._classify_device_change(zone, mystery_dev, {"foo": "bar"})
        assert record["role"] == "unknown"


class TestFormatChangeLine:
    """Tests for _format_change_line."""

    def test_temperature_with_old_and_new(self):
        from auto_fan.auto_fan_agent import _format_change_line
        line = _format_change_line({"role": "temperature", "device_name": "Temp",
                                    "old_value": 67.0, "new_value": 66.5})
        assert "67.0°F" in line
        assert "66.5°F" in line
        assert "🌡️" in line

    def test_humidity_with_old_and_new(self):
        from auto_fan.auto_fan_agent import _format_change_line
        line = _format_change_line({"role": "humidity", "device_name": "Hum",
                                    "old_value": 54.0, "new_value": 56.0})
        assert "54%" in line
        assert "56%" in line
        assert "💧" in line

    def test_presence_detected(self):
        from auto_fan.auto_fan_agent import _format_change_line
        line = _format_change_line({"role": "presence", "device_name": "Motion",
                                    "old_value": False, "new_value": True})
        assert "detected" in line
        assert "👤" in line

    def test_presence_not_detected(self):
        from auto_fan.auto_fan_agent import _format_change_line
        line = _format_change_line({"role": "presence", "device_name": "Motion",
                                    "old_value": True, "new_value": False})
        assert "not detected" in line

    def test_weather_with_old_and_new(self):
        from auto_fan.auto_fan_agent import _format_change_line
        line = _format_change_line({"role": "weather", "device_name": "Weather",
                                    "old_value": 85.0, "new_value": 87.0})
        assert "85.0°F" in line
        assert "87.0°F" in line
        assert "🌤️" in line

    def test_thermostat_with_diff(self):
        from auto_fan.auto_fan_agent import _format_change_line
        line = _format_change_line({"role": "thermostat", "device_name": "HVAC",
                                    "diff": {"coolSetpoint": 74.0}})
        assert "coolSetpoint" in line
        assert "🏠" in line

    def test_unknown_role(self):
        from auto_fan.auto_fan_agent import _format_change_line
        line = _format_change_line({"role": "unknown", "device_name": "Mystery",
                                    "old_value": None, "new_value": None})
        assert "Mystery" in line
        assert "📡" in line


class TestSpeedIndexPhantomSuppression:
    """Tests for speed index-aware change detection."""

    def test_phantom_change_suppressed_same_index(self, fake_indigo, speed_control_calls):
        """Target 20% on a 4-speed fan at index 1 (33%) should NOT trigger a change."""
        _setup_zone_devices(fake_indigo, temp_value=73.0, fan_speed=33, speed_index_count=4)
        agent = _make_agent()
        zone = agent.config.zones[0]

        # Fan is at index 1 (33%). Curve produces ~20% target.
        # 20% maps to index 1 — same as current. No change should happen.
        result = agent.process_zone(zone)
        assert result is True
        # Target should be ~20% (delta=1, curve: 1/5*100=20)
        # But index 1 = index 1, so no speed control call
        assert len(speed_control_calls) == 0

    def test_real_change_detected_different_index(self, fake_indigo, speed_control_calls):
        """Target that maps to a different index should trigger a change."""
        _setup_zone_devices(fake_indigo, temp_value=77.0, fan_speed=0, speed_index_count=4)
        agent = _make_agent()
        zone = agent.config.zones[0]

        # Fan is at index 0 (0%). Temp=77, ideal=72, delta=5 → 100% → index 3.
        result = agent.process_zone(zone)
        assert result is True
        assert len(speed_control_calls) > 0

    def test_phantom_zero_to_ten_suppressed(self, fake_indigo, speed_control_calls):
        """The Master Bedroom phantom: 0% → 10% on a 4-speed fan should be suppressed."""
        _setup_zone_devices(fake_indigo, temp_value=72.5, fan_speed=0, speed_index_count=4)
        agent = _make_agent()
        zone = agent.config.zones[0]

        # delta=0.5, curve: 0.5/5*100=10%. Index for 10% = round(10*3/100) = 0.
        # Current index = 0. Same — no change.
        result = agent.process_zone(zone)
        assert result is True
        assert len(speed_control_calls) == 0

    def test_dimmer_fan_uses_percentage_comparison(self, fake_indigo, speed_control_calls):
        """Dimmer devices (no speedIndex) should fall back to percentage comparison."""
        _setup_zone_devices(fake_indigo, temp_value=75.0, fan_speed=0, speed_index_count=None)
        # Remove speedIndex attrs to simulate a dimmer
        fan_dev = fake_indigo.devices[100]
        fan_dev.speedIndex = None
        fan_dev.speedIndexCount = None
        fan_dev.speedLevel = 0
        fan_dev.brightness = 0

        agent = _make_agent()
        zone = agent.config.zones[0]

        result = agent.process_zone(zone)
        assert result is True
        assert len(speed_control_calls) > 0


class TestSpeedChangeDescription:
    """Tests for get_speed_change_description."""

    def test_speed_control_shows_index_mapped_pct(self, fake_indigo):
        """SpeedControl should show index-mapped percentages, not raw target."""
        _setup_zone_devices(fake_indigo, temp_value=77.0, fan_speed=33, speed_index_count=4)
        agent = _make_agent()
        zone = agent.config.zones[0]
        zone.calculate_target_speed()

        from_str, to_str = zone.get_speed_change_description()
        # Current: index 1 → 33%. Target 100% → index 3 → 100%.
        assert from_str == "33%"
        assert to_str == "100%"

    def test_speed_control_off_to_low(self, fake_indigo):
        """Verify off → low shows 0% → 33%."""
        _setup_zone_devices(fake_indigo, temp_value=74.0, fan_speed=0, speed_index_count=4)
        agent = _make_agent()
        zone = agent.config.zones[0]
        zone.calculate_target_speed()

        from_str, to_str = zone.get_speed_change_description()
        assert from_str == "0%"
        # delta=2, curve: 2/5*100=40% → index round(40*3/100)=round(1.2)=1 → 33%
        assert to_str == "33%"


class TestStructuredLogOutput:
    """Tests for the new structured log format."""

    def test_speed_change_produces_structured_log(self, fake_indigo, speed_control_calls, caplog):
        """Speed change should produce multi-line structured log with tabs."""
        import logging
        _setup_zone_devices(fake_indigo, temp_value=77.0, fan_speed=0, speed_index_count=4)
        agent = _make_agent()
        zone = agent.config.zones[0]

        with caplog.at_level(logging.INFO, logger="Plugin"):
            agent.process_zone(zone)

        # Should have structured output with emojis and tabs
        messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("🌀 Zone" in m for m in messages), "Missing header line"
        assert any("\t📝 Calculation:" in m for m in messages), "Missing calculation section"
        assert any("\t⚙️ Changes:" in m for m in messages), "Missing changes section"

    def test_debounced_change_shows_triggers(self, fake_indigo, speed_control_calls, caplog):
        """Debounced changes should show trigger lines with tab indentation."""
        import logging
        _setup_zone_devices(fake_indigo, temp_value=75.0, fan_speed=0, speed_index_count=4)
        agent = _make_agent()
        zone = agent.config.zones[0]

        # Queue a change and process
        temp_dev = fake_indigo.devices[200]
        agent.process_device_change(temp_dev, {"sensorValue": 76.0})
        agent._debounce_timers["Living Room"].cancel()

        with caplog.at_level(logging.INFO, logger="Plugin"):
            agent._process_debounced_zone(zone)

        messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("\t🔄" in m for m in messages), "Missing trigger line"

    def test_no_speed_change_no_info_log(self, fake_indigo, speed_control_calls, caplog):
        """When speed index is unchanged, no INFO log should be produced."""
        import logging
        # Fan at 33% (index 1), temp produces ~20% target (also index 1)
        _setup_zone_devices(fake_indigo, temp_value=73.0, fan_speed=33, speed_index_count=4)
        agent = _make_agent()
        zone = agent.config.zones[0]

        with caplog.at_level(logging.INFO, logger="Plugin"):
            agent.process_zone(zone)

        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert not any("🌀 Zone" in m for m in info_messages), \
            "Should not log speed change when index unchanged"

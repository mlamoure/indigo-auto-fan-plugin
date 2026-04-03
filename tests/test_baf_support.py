"""Tests for BAF/Haiku native 8-speed fan support."""
import pytest
from conftest import Device, PluginStub

import indigo
from auto_fan.utils import (
    is_baf_fan, send_fan_speed, get_fan_speed_pct,
    BAF_PLUGIN_ID, BAF_SPEED_COUNT,
)
from auto_fan.fan_zone import FanZone
from auto_fan.auto_fan_agent import SPEED_CHANGE_KEYS


def _make_baf_fan(dev_id, name="BAF Fan", baf_speed=0):
    """Create a BAF fan device with proper metadata."""
    dev = Device(dev_id, name=name, speedLevel=0, speedIndex=0, speedIndexCount=4)
    dev.pluginId = BAF_PLUGIN_ID
    dev.deviceTypeId = "bafFan"
    dev.states["baf_speed"] = baf_speed
    return dev


def _make_zone(fan_dev_id=100):
    """Create a FanZone with a single temp sensor."""
    from auto_fan.auto_fan_config import AutoFanConfig
    import json, tempfile, os

    conf = {
        "plugin_config": {
            "enabled": True,
            "default_lock_duration": 60,
            "default_lock_extension_duration": 30,
        },
        "zones": [
            {
                "name": "BAF Test Zone",
                "fan_dev_id": fan_dev_id,
                "temp_sensor_dev_ids": [200],
                "presence_dev_ids": [],
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

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(conf, tmp)
    tmp.close()

    config = AutoFanConfig(tmp.name)
    os.unlink(tmp.name)
    return config.zones[0], config


# ---- Detection ----


class TestIsBAFFan:
    def test_baf_fan_detected(self, fake_indigo):
        dev = _make_baf_fan(100)
        assert is_baf_fan(dev) is True

    def test_standard_speed_control_not_detected(self, fake_indigo):
        dev = Device(100, speedLevel=50, speedIndex=1, speedIndexCount=4)
        assert is_baf_fan(dev) is False

    def test_wrong_plugin_id(self, fake_indigo):
        dev = Device(100, speedLevel=50)
        dev.pluginId = "some.other.plugin"
        dev.deviceTypeId = "bafFan"
        assert is_baf_fan(dev) is False

    def test_wrong_device_type(self, fake_indigo):
        dev = Device(100, speedLevel=50)
        dev.pluginId = BAF_PLUGIN_ID
        dev.deviceTypeId = "bafLight"
        assert is_baf_fan(dev) is False

    def test_no_attributes(self, fake_indigo):
        """Device without pluginId/deviceTypeId attributes."""
        dev = object()
        assert is_baf_fan(dev) is False


# ---- Speed Mapping (via send_fan_speed) ----


class TestBAFSpeedMapping:
    """Verify percentage-to-BAF-speed mapping through send_fan_speed."""

    @pytest.mark.parametrize("pct, expected_speed", [
        (0.0, "0"),
        (7.0, "0"),      # rounds to 0
        (8.0, "1"),       # rounds to 1
        (14.3, "1"),
        (28.6, "2"),
        (42.9, "3"),
        (50.0, "4"),      # midpoint
        (57.1, "4"),
        (71.4, "5"),
        (85.7, "6"),
        (100.0, "7"),
    ])
    def test_mapping(self, fake_indigo, plugin_registry, pct, expected_speed):
        fake_indigo.devices[100] = _make_baf_fan(100)
        send_fan_speed(100, pct)
        action = plugin_registry[BAF_PLUGIN_ID].executed_actions[0]
        assert action["props"]["speed"] == expected_speed

    def test_pct_to_speed_index_with_8_count(self):
        """FanZone._pct_to_speed_index works correctly with 8 speeds."""
        assert FanZone._pct_to_speed_index(0.0, 8) == 0
        assert FanZone._pct_to_speed_index(50.0, 8) == 4
        assert FanZone._pct_to_speed_index(100.0, 8) == 7


# ---- Speed Info ----


class TestGetDeviceSpeedInfoBAF:
    def test_returns_8_speed_count(self, fake_indigo):
        fake_indigo.devices[100] = _make_baf_fan(100, baf_speed=3)
        zone, _ = _make_zone()
        info = zone._get_device_speed_info()
        assert info["speed_index_count"] == 8
        assert info["speed_index"] == 3

    def test_speed_0(self, fake_indigo):
        fake_indigo.devices[100] = _make_baf_fan(100, baf_speed=0)
        zone, _ = _make_zone()
        info = zone._get_device_speed_info()
        assert info["speed_index"] == 0

    def test_speed_7(self, fake_indigo):
        fake_indigo.devices[100] = _make_baf_fan(100, baf_speed=7)
        zone, _ = _make_zone()
        info = zone._get_device_speed_info()
        assert info["speed_index"] == 7

    def test_standard_speed_control_unaffected(self, fake_indigo):
        """Non-BAF SpeedControl still returns Indigo's speedIndex."""
        fake_indigo.devices[100] = Device(
            100, speedLevel=50, speedIndex=2, speedIndexCount=4
        )
        zone, _ = _make_zone()
        info = zone._get_device_speed_info()
        assert info["speed_index_count"] == 4
        assert info["speed_index"] == 2


# ---- Read Current Speed ----


class TestGetFanSpeedPctBAF:
    def test_reads_baf_speed_not_speed_level(self, fake_indigo):
        dev = _make_baf_fan(100, baf_speed=5)
        dev.speedLevel = 18  # Indigo's quantized value — should be ignored
        fake_indigo.devices[100] = dev
        pct = get_fan_speed_pct(100)
        assert pct == round(5 * 100.0 / 7)  # 71%

    def test_speed_0_returns_0(self, fake_indigo):
        fake_indigo.devices[100] = _make_baf_fan(100, baf_speed=0)
        assert get_fan_speed_pct(100) == 0

    def test_speed_7_returns_100(self, fake_indigo):
        fake_indigo.devices[100] = _make_baf_fan(100, baf_speed=7)
        assert get_fan_speed_pct(100) == 100

    @pytest.mark.parametrize("baf_speed, expected_pct", [
        (0, 0), (1, 14), (2, 29), (3, 43), (4, 57), (5, 71), (6, 86), (7, 100),
    ])
    def test_all_speeds(self, fake_indigo, baf_speed, expected_pct):
        fake_indigo.devices[100] = _make_baf_fan(100, baf_speed=baf_speed)
        assert get_fan_speed_pct(100) == expected_pct


# ---- Send Speed ----


class TestSendFanSpeedBAF:
    def test_calls_baf_plugin_action(self, fake_indigo, plugin_registry):
        fake_indigo.devices[100] = _make_baf_fan(100)
        result = send_fan_speed(100, 50.0)
        assert result is True

        baf_stub = plugin_registry[BAF_PLUGIN_ID]
        assert len(baf_stub.executed_actions) == 1
        action = baf_stub.executed_actions[0]
        assert action["action_id"] == "setBAFFanSpeed"
        assert action["deviceId"] == 100
        assert action["props"]["speed"] == "4"  # 50% → speed 4

    def test_0_pct_sends_speed_0(self, fake_indigo, plugin_registry):
        fake_indigo.devices[100] = _make_baf_fan(100)
        send_fan_speed(100, 0.0)
        action = plugin_registry[BAF_PLUGIN_ID].executed_actions[0]
        assert action["props"]["speed"] == "0"

    def test_100_pct_sends_speed_7(self, fake_indigo, plugin_registry):
        fake_indigo.devices[100] = _make_baf_fan(100)
        send_fan_speed(100, 100.0)
        action = plugin_registry[BAF_PLUGIN_ID].executed_actions[0]
        assert action["props"]["speed"] == "7"

    def test_does_not_use_speed_control_api(self, fake_indigo, speed_control_calls, plugin_registry):
        fake_indigo.devices[100] = _make_baf_fan(100)
        send_fan_speed(100, 50.0)
        assert len(speed_control_calls) == 0  # Should NOT use speedcontrol API

    def test_fallback_when_baf_plugin_disabled(self, fake_indigo, speed_control_calls, plugin_registry):
        fake_indigo.devices[100] = _make_baf_fan(100)
        # Pre-register a disabled BAF plugin stub
        plugin_registry[BAF_PLUGIN_ID] = PluginStub(BAF_PLUGIN_ID, enabled=False)
        send_fan_speed(100, 50.0)
        # Should fall back to standard speedcontrol
        assert len(speed_control_calls) == 1
        assert speed_control_calls[0]["value"] == 50

    def test_standard_speed_control_unaffected(self, fake_indigo, speed_control_calls):
        """Non-BAF SpeedControl still uses standard API."""
        fake_indigo.devices[100] = Device(
            100, name="Regular Fan", speedLevel=0, speedIndex=0, speedIndexCount=4
        )
        send_fan_speed(100, 67.0)
        assert len(speed_control_calls) == 1
        assert speed_control_calls[0]["value"] == 67


# ---- Change Detection ----


class TestHasSpeedChangeBAF:
    def test_same_baf_index_no_change(self, fake_indigo):
        """14% target on BAF at speed 1 (14%) → no change."""
        fake_indigo.devices[100] = _make_baf_fan(100, baf_speed=1)
        fake_indigo.devices[200] = Device(200, sensorValue=73.0)
        fake_indigo.devices[200].states["sensorValue"] = 73.0
        zone, _ = _make_zone()
        zone._target_speed_pct = 14.3  # Maps to index 1
        assert zone.has_speed_change() is False

    def test_different_baf_index_detects_change(self, fake_indigo):
        """50% target on BAF at speed 1 → change detected."""
        fake_indigo.devices[100] = _make_baf_fan(100, baf_speed=1)
        fake_indigo.devices[200] = Device(200, sensorValue=75.0)
        fake_indigo.devices[200].states["sensorValue"] = 75.0
        zone, _ = _make_zone()
        zone._target_speed_pct = 50.0  # Maps to index 4
        assert zone.has_speed_change() is True

    def test_8_speed_granularity(self, fake_indigo):
        """Two percentages that map to different BAF indices but same Indigo index."""
        fake_indigo.devices[100] = _make_baf_fan(100, baf_speed=2)
        fake_indigo.devices[200] = Device(200, sensorValue=74.0)
        fake_indigo.devices[200].states["sensorValue"] = 74.0
        zone, _ = _make_zone()
        # 43% maps to BAF index 3, current is 2 → should detect change
        # But with Indigo's 4-speed, both would map to index 1
        zone._target_speed_pct = 43.0
        assert zone.has_speed_change() is True


# ---- Speed Change Description ----


class TestSpeedChangeDescriptionBAF:
    def test_uses_8_speed_percentages(self, fake_indigo):
        fake_indigo.devices[100] = _make_baf_fan(100, baf_speed=2)
        zone, _ = _make_zone()
        zone._target_speed_pct = 71.0  # Maps to BAF index 5
        from_str, to_str = zone.get_speed_change_description()
        assert from_str == "29%"   # index 2 of 8 → 29%
        assert to_str == "71%"     # index 5 of 8 → 71%


# ---- Manual Override Detection ----


class TestSpeedChangeKeysBAF:
    def test_baf_speed_in_speed_change_keys(self):
        """baf_speed must be in SPEED_CHANGE_KEYS for manual override detection."""
        assert "baf_speed" in SPEED_CHANGE_KEYS

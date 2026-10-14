"""Tests for the Phase 2 lock model: nighttime locks, presence extension,
re-adjustment, away release and persistence."""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from conftest import Device, Variable
from test_auto_fan_agent import _make_agent, _setup_zone_devices


FALL = patch("auto_fan.fan_zone.get_current_season", return_value="fall")


def _agent_with_night_zone(fake_indigo, presence=True, hold="inherit"):
    _setup_zone_devices(fake_indigo, temp_value=75.0, fan_speed=0, presence=presence)
    agent = _make_agent()
    zone = agent.config.zones[0]
    zone.night_window["fall"] = {"start_hour": 20, "end_hour": 8}
    zone.hold_locks_at_night = hold
    return agent, zone


class TestLockRuleSelection:
    def test_night_change_locks_until_night_end(self, fake_indigo, fake_clock):
        agent, zone = _agent_with_night_zone(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 22, 22))
        with FALL:
            zone.lock_zone("manual change on 'Fan'")
        assert zone.locked and zone.lock_rule == "night_end"
        assert zone.lock_expiration == datetime(2026, 10, 4, 8, 0)
        assert zone.lock_step == 0

    def test_day_change_locks_for_duration(self, fake_indigo, fake_clock):
        agent, zone = _agent_with_night_zone(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            zone.lock_zone("manual change")
        assert zone.lock_rule == "duration"
        assert zone.lock_expiration == datetime(2026, 10, 3, 15, 0)  # default 60 min

    def test_zone_override_off_uses_duration_at_night(self, fake_indigo, fake_clock):
        agent, zone = _agent_with_night_zone(fake_indigo, hold="off")
        fake_clock.set(datetime(2026, 10, 3, 22, 22))
        with FALL:
            zone.lock_zone("manual change")
        assert zone.lock_rule == "duration"

    def test_plugin_default_off_but_zone_on(self, fake_indigo, fake_clock):
        agent, zone = _agent_with_night_zone(fake_indigo, hold="on")
        agent.config.hold_locks_at_night = False
        fake_clock.set(datetime(2026, 10, 3, 23, 0))
        with FALL:
            zone.lock_zone("manual change")
        assert zone.lock_rule == "night_end"

    def test_plugin_default_off_inherit_uses_duration(self, fake_indigo, fake_clock):
        agent, zone = _agent_with_night_zone(fake_indigo)
        agent.config.hold_locks_at_night = False
        fake_clock.set(datetime(2026, 10, 3, 23, 0))
        with FALL:
            zone.lock_zone("manual change")
        assert zone.lock_rule == "duration"


class TestExpiry:
    def test_night_lock_releases_at_night_end_even_with_presence(self, fake_indigo, fake_clock, speed_control_calls):
        agent, zone = _agent_with_night_zone(fake_indigo, presence=True)
        fake_clock.set(datetime(2026, 10, 3, 22, 22))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])
            fake_clock.set(datetime(2026, 10, 4, 8, 0, 3))
            speed_control_calls.clear()
            agent.process_expired_lock(zone)
        assert zone.locked is False
        assert len(speed_control_calls) == 1  # automation resumed

    def test_duration_lock_extends_while_presence(self, fake_indigo, fake_clock):
        agent, zone = _agent_with_night_zone(fake_indigo, presence=True)
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])
            fake_clock.set(datetime(2026, 10, 3, 15, 0, 3))
            agent.process_expired_lock(zone)
        assert zone.locked is True
        assert zone.lock_expiration == datetime(2026, 10, 3, 15, 30, 3)  # +30 min extension
        assert zone.lock_extensions == 1

    def test_duration_lock_unlocks_without_presence(self, fake_indigo, fake_clock):
        agent, zone = _agent_with_night_zone(fake_indigo, presence=False)
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])
            fake_clock.set(datetime(2026, 10, 3, 15, 0, 3))
            agent.process_expired_lock(zone)
        assert zone.locked is False

    def test_extension_capped_at_max_lock_hours(self, fake_indigo, fake_clock):
        agent, zone = _agent_with_night_zone(fake_indigo, presence=True)
        agent.config.max_lock_hours = 2
        fake_clock.set(datetime(2026, 10, 3, 10, 0))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])
            # 11:00 → extend to 11:30, 11:30 → 12:00 (cap), 12:00 → cannot extend → unlock
            for t in (datetime(2026, 10, 3, 11, 0, 3), datetime(2026, 10, 3, 11, 30, 3)):
                fake_clock.set(t)
                agent.process_expired_lock(zone)
                assert zone.locked is True
            assert zone.lock_expiration == datetime(2026, 10, 3, 12, 0)
            fake_clock.set(datetime(2026, 10, 3, 12, 0, 3))
            agent.process_expired_lock(zone)
        assert zone.locked is False

    def test_early_timer_fire_rearms(self, fake_indigo, fake_clock):
        agent, zone = _agent_with_night_zone(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])
            fake_clock.set(datetime(2026, 10, 3, 14, 30))
            agent.process_expired_lock(zone)
        assert zone.locked is True and zone.name in agent._timers


class TestReadjustAndEcho:
    def test_second_manual_change_restarts_lock(self, fake_indigo, fake_clock):
        agent, zone = _agent_with_night_zone(fake_indigo)
        fan = fake_indigo.devices[100]
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            fan.speedIndex, fan.speedLevel = 2, 67
            agent.process_device_change(fan, {"speedLevel": 67})
            assert zone.locked and zone.lock_expiration == datetime(2026, 10, 3, 15, 0)
            fake_clock.set(datetime(2026, 10, 3, 14, 40))
            fan.speedIndex, fan.speedLevel = 3, 100
            agent.process_device_change(fan, {"speedLevel": 100})
        assert zone.lock_expiration == datetime(2026, 10, 3, 15, 40)
        assert "re-adjustment" in zone.lock_reason
        assert zone.lock_step == 3

    def test_echo_of_locked_step_does_nothing(self, fake_indigo, fake_clock):
        agent, zone = _agent_with_night_zone(fake_indigo)
        fan = fake_indigo.devices[100]
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            fan.speedIndex, fan.speedLevel = 2, 67
            agent.process_device_change(fan, {"speedLevel": 67})
            first_expiration = zone.lock_expiration
            fake_clock.set(datetime(2026, 10, 3, 14, 1))
            agent.process_device_change(fan, {"speedLevel": 67})
        assert zone.lock_expiration == first_expiration


class TestAwayRelease:
    def test_locked_zone_releases_when_house_goes_away(self, fake_indigo, fake_clock, speed_control_calls):
        agent, zone = _agent_with_night_zone(fake_indigo)
        fake_indigo.variables[900] = Variable(900, name="home", value="true")
        agent.config.away_var_id = 900
        agent.config.away_var_home_value = "true"
        fake_clock.set(datetime(2026, 10, 3, 22, 30))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])
            assert zone.locked
            fake_indigo.variables[900].value = "false"
            agent.process_variable_change(fake_indigo.variables[900], fake_indigo.variables[900])
        assert zone.locked is False
        assert zone.name not in agent._timers


class TestPersistence:
    def test_lock_written_to_state_store(self, fake_indigo, fake_clock):
        agent, zone = _agent_with_night_zone(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 22, 22))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])
        saved = agent.config.state_store.load("locks")
        assert saved["Living Room"]["rule"] == "night_end"
        assert saved["Living Room"]["until"] == "2026-10-04T08:00:00"
        agent.reset_locks("Living Room")
        assert agent.config.state_store.load("locks") == {}

    def test_restore_on_new_agent(self, fake_indigo, fake_clock):
        from auto_fan.auto_fan_agent import AutoFanAgent
        agent, zone = _agent_with_night_zone(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 22, 22))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])
            agent.shutdown()
            # a "reload": new agent over the same config object (same in-memory store)
            zone.locked = False
            agent2 = AutoFanAgent(agent.config)
        z2 = agent2.config.zones[0]
        assert z2.locked is True and z2.lock_rule == "night_end"
        assert z2.lock_expiration == datetime(2026, 10, 4, 8, 0)
        assert z2.name in agent2._timers
        agent2.shutdown()

    def test_expired_entry_dropped_on_restore(self, fake_indigo, fake_clock):
        from auto_fan.auto_fan_agent import AutoFanAgent
        agent, zone = _agent_with_night_zone(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 22, 22))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])
            agent.shutdown()
            zone.locked = False
            fake_clock.set(datetime(2026, 10, 4, 9, 0))
            agent2 = AutoFanAgent(agent.config)
        assert agent2.config.zones[0].locked is False
        assert agent2.config.state_store.load("locks") == {}
        agent2.shutdown()


class TestConfigLoading:
    def test_zone_and_plugin_settings_load(self, fake_indigo):
        from auto_fan.auto_fan_config import AutoFanConfig
        import json, tempfile, os
        conf = {"plugin_config": {"hold_locks_at_night": False, "max_lock_hours": 6},
                "zones": [{"name": "Z", "temp_sensor_dev_ids": [1], "hold_locks_at_night": "on"}]}
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(conf, tmp); tmp.close()
        config = AutoFanConfig(tmp.name); os.unlink(tmp.name)
        assert config.hold_locks_at_night is False and config.max_lock_hours == 6
        assert config.zones[0].hold_locks_at_night == "on"
        assert config.zones[0].holds_locks_at_night() is True

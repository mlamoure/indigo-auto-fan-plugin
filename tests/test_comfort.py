"""Tests for comfort offsets (Phase 4): curve inversion, derivation from a
manual change, expiry rules, persistence, the action API and the record."""
from datetime import datetime
from unittest.mock import patch

import pytest

from conftest import Variable
from auto_fan.comfort import implied_delta_from_speed, round_offset, temporary_offset_for_change
from test_auto_fan_agent import _make_agent, _setup_zone_devices

FALL = patch("auto_fan.fan_zone.get_current_season", return_value="fall")

# the Master Bedroom fall curve in effect during the analysed week
MB_FALL = [{"offset": -3, "speed": 6}, {"offset": -2, "speed": 15}, {"offset": -1, "speed": 25},
           {"offset": 0, "speed": 34}, {"offset": 1, "speed": 46}, {"offset": 2, "speed": 56}, {"offset": 3, "speed": 65}]


class TestInversion:
    @pytest.mark.parametrize("pct,delta", [(34, 0.0), (46, 1.0), (40, 0.5), (29, -0.56), (57, 2.11), (6, -3.0)])
    def test_interpolates_on_the_curve(self, pct, delta):
        assert implied_delta_from_speed(pct, MB_FALL) == pytest.approx(delta, abs=0.01)

    def test_beyond_the_ends_clamps_to_the_curve_end(self):
        assert implied_delta_from_speed(100, MB_FALL) == 3.0   # flat beyond 65%
        assert implied_delta_from_speed(0, MB_FALL) == -3.0

    def test_flat_segment_uses_midpoint(self):
        pts = [{"offset": -3, "speed": 0}, {"offset": -2, "speed": 0}, {"offset": -1, "speed": 14}, {"offset": 0, "speed": 29}]
        assert implied_delta_from_speed(0, pts) == -3.0        # lowest offset at the minimum
        assert implied_delta_from_speed(7, pts) == pytest.approx(-1.5)

    def test_empty_curve(self):
        assert implied_delta_from_speed(50, []) is None

    def test_round_offset(self):
        assert round_offset(-2.01) == -2.0
        assert round_offset(-2.3) == -2.5
        assert round_offset(4.0) == 3.0
        assert round_offset(-3.7) == -3.0
        assert round_offset(0.2) == 0.0

    def test_last_weeks_change(self):
        # 10/3 22:22: room 72.1, ideal 72, person chose 57% → implied ideal ≈ 70.0, offset −2.0
        implied, offset = temporary_offset_for_change(57, MB_FALL, 72.1, 72.0)
        assert implied == pytest.approx(70.0, abs=0.05) and offset == -2.0

    def test_additive_boost_removed_first(self):
        # humidity +10 was active: the person effectively chose 33% on the curve
        implied, offset = temporary_offset_for_change(43, MB_FALL, 68.3, 72.0, additive_boost=10)
        assert implied == pytest.approx(68.41, abs=0.05) and offset == -3.0

    def test_missing_inputs(self):
        assert temporary_offset_for_change(57, MB_FALL, None, 72.0) is None
        assert temporary_offset_for_change(57, MB_FALL, 72.0, None) is None


def _agent(fake_indigo, temp=72.1, presence=True):
    _setup_zone_devices(fake_indigo, temp_value=temp, fan_speed=0, presence=presence)
    agent = _make_agent()
    zone = agent.config.zones[0]
    zone.night_window["fall"] = {"start_hour": 20, "end_hour": 8}
    zone.seasonal_curves["fall"] = {"temperature_range": 3, "num_points": 7, "points": [dict(p) for p in MB_FALL]}
    zone.seasonal_ideal_temp["fall"] = {"source": "static", "value": 72.0, "var_id": None}
    return agent, zone


class TestDerivedOffset:
    def test_night_change_sets_offset_until_night_end(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        fan = fake_indigo.devices[100]
        fake_clock.set(datetime(2026, 10, 3, 22, 22))
        with FALL:
            agent.process_zone(zone)
            fake_clock.advance(seconds=30)
            fan.speedIndex, fan.speedLevel = 3, 100      # 4-step fan: 100% ≈ step 3
            agent.process_device_change(fan, {"speedLevel": 100})
            agent.flush_settling_changes()  # the fan settled (normally 30 s later)
            assert zone.temporary_offset == -3.0         # 100% is beyond the curve → +3 → 69.1 → −3
            assert zone.temporary_offset_expires == datetime(2026, 10, 4, 8, 0)
            assert zone.get_ideal_temperature() == 69.0
            assert zone.get_configured_ideal_temperature() == 72.0
        assert agent.config.state_store.load("offsets")["Living Room"]["offset"] == -3.0

    def test_day_change_lasts_until_night_begins(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        fan = fake_indigo.devices[100]
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            fan.speedIndex, fan.speedLevel = 2, 67
            agent.process_device_change(fan, {"speedLevel": 67})
            agent.flush_settling_changes()  # the fan settled (normally 30 s later)
        assert zone.temporary_offset is not None
        assert zone.temporary_offset_expires == datetime(2026, 10, 3, 20, 0)

    def test_offset_survives_lock_release_and_shapes_the_resume(self, fake_indigo, fake_clock, speed_control_calls):
        """The 02:22 problem: the lock ends but the person's preference does not."""
        agent, zone = _agent(fake_indigo, presence=False)
        zone.hold_locks_at_night = "off"              # force a duration lock to see the resume
        fan = fake_indigo.devices[100]
        fake_clock.set(datetime(2026, 10, 3, 22, 22))
        with FALL:
            agent.process_zone(zone)                  # Δ=+0.1 → 35% → step 1 (33%)
            fake_clock.advance(seconds=30)
            fan.speedIndex, fan.speedLevel = 2, 67    # person raises one step (67% is past the curve's 65% top)
            agent.process_device_change(fan, {"speedLevel": 67})
            agent.flush_settling_changes()  # the fan settled (normally 30 s later)
            assert zone.temporary_offset == -3.0      # implied Δ +3 → ideal 69.1 → −3.0
            fake_clock.set(datetime(2026, 10, 3, 23, 23))
            speed_control_calls.clear()
            agent.process_expired_lock(zone)
        # with ideal 69.0 and room 72.1, Δ=+3.1 → 65% → step 2: the fan stays where the person put it
        assert zone.locked is False
        assert zone._last_commanded_step == 2

    def test_offset_expires_and_zone_reevaluates(self, fake_indigo, fake_clock, speed_control_calls):
        agent, zone = _agent(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 22, 0))
        with FALL:
            agent.set_comfort_offset("Living Room", -2.0, until="night_end", source="test")
            assert zone.temporary_offset_active()
            fake_clock.set(datetime(2026, 10, 4, 8, 0, 3))
            speed_control_calls.clear()
            agent.process_offset_expiry(zone)
        assert zone.temporary_offset is None
        assert zone.get_ideal_temperature() == 72.0
        assert agent.config.state_store.load("offsets") == {}

    def test_restore_on_new_agent(self, fake_indigo, fake_clock):
        from auto_fan.auto_fan_agent import AutoFanAgent
        agent, zone = _agent(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 22, 0))
        with FALL:
            agent.set_comfort_offset("Living Room", -1.5, until="night_end", source="test")
            agent.shutdown()
            zone.temporary_offset = None
            agent2 = AutoFanAgent(agent.config)
        z2 = agent2.config.zones[0]
        assert z2.temporary_offset == -1.5 and z2.temporary_offset_expires == datetime(2026, 10, 4, 8, 0)
        agent2.shutdown()


class TestOffsetApi:
    @pytest.mark.parametrize("until,expected", [
        ("night_end", datetime(2026, 10, 4, 8, 0)),
        ("night_start", datetime(2026, 10, 3, 20, 0)),
        ("daily_reset", datetime(2026, 10, 4, 10, 0)),
        ("hours", datetime(2026, 10, 3, 18, 0)),
    ])
    def test_until_choices(self, fake_indigo, fake_clock, until, expected):
        agent, zone = _agent(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            assert agent.set_comfort_offset("Living Room", -1.0, until=until, hours=4) is True
        assert zone.temporary_offset == -1.0 and zone.temporary_offset_expires == expected

    def test_daily_reset_honours_config(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        agent.config.daily_reset_hour = 7
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            agent.set_comfort_offset("Living Room", 1.0, until="daily_reset")
        assert zone.temporary_offset_expires == datetime(2026, 10, 4, 7, 0)

    def test_zero_offset_clears(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        with FALL:
            agent.set_comfort_offset("Living Room", -1.0)
            agent.set_comfort_offset("Living Room", 0.0)
        assert zone.temporary_offset is None

    def test_rounding_and_clamp(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        with FALL:
            agent.set_comfort_offset("Living Room", -7.3)
        assert zone.temporary_offset == -3.0

    def test_unknown_zone(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        assert agent.set_comfort_offset("Nope", -1.0) is False
        assert agent.clear_comfort_offset("Nope") is False

    def test_clear(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        with FALL:
            agent.set_comfort_offset("Living Room", -1.0)
            assert agent.clear_comfort_offset("Living Room") is True
        assert zone.temporary_offset is None and zone.name not in agent._offset_timers

    def test_zone_status(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 22, 0))
        with FALL:
            agent.set_comfort_offset("Living Room", -2.0, until="night_end", source="web page")
            st = agent.zone_status(zone)
        assert st["temporary_offset"] == -2.0 and st["temporary_offset_expires"] == "Sun 08:00"
        assert st["configured_ideal"] == 72.0 and st["effective_ideal"] == 70.0
        assert st["locked"] is False and st["is_night"] is True


class TestRecordCarriesOffsets:
    def test_override_record_has_offset_fields(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 22, 0))
        with FALL:
            agent.set_comfort_offset("Living Room", -1.0, until="night_end")
            fan = fake_indigo.devices[100]
            fake_clock.advance(seconds=30)
            fan.speedIndex, fan.speedLevel = 3, 100
            agent.process_device_change(fan, {"speedLevel": 100})
            agent.flush_settling_changes()  # the fan settled (normally 30 s later)
        rec = [r for r in agent.override_log.iter_records() if r["type"] == "override"][0]
        assert rec["ideal_configured"] == 72.0 and rec["offset_temporary"] == -1.0 and rec["ideal"] == 71.0


class TestOffsetEndsWithNightLock:
    def test_offset_ended_by_the_lock_release_is_dropped_from_saved_state(self, fake_indigo, fake_clock, speed_control_calls):
        from auto_fan.auto_fan_agent import LOCK_EXPIRY_GRACE_SECONDS, OFFSET_EXPIRY_GRACE_SECONDS
        assert OFFSET_EXPIRY_GRACE_SECONDS > LOCK_EXPIRY_GRACE_SECONDS  # the lock's evaluation goes first
        agent, zone = _agent(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 22, 0))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])  # night lock and offset, both until 08:00
            assert agent.config.state_store.load("offsets")
            fake_clock.set(datetime(2026, 10, 4, 8, 0, 2))
            speed_control_calls.clear()
            agent.process_expired_lock(zone)          # unlocks; its evaluation ends the offset
            assert zone.temporary_offset is None
            commands = len(speed_control_calls)
            fake_clock.set(datetime(2026, 10, 4, 8, 0, 5))
            agent.process_offset_expiry(zone)         # the offset timer, three seconds later
        assert agent.config.state_store.load("offsets") == {}
        assert len(speed_control_calls) == commands   # no second command
        agent.shutdown()


class TestCurveEnds:
    """At either end of the curve a manual change gives a bound, not a value."""

    def test_off_in_a_cold_room_says_nothing(self):
        # A bedroom: room 63, ideal 69, curve floor reached long ago; turned off
        assert temporary_offset_for_change(0, MB_FALL, 63.0, 69.0) is None

    def test_off_in_a_warm_room_still_means_warmer(self):
        # room 70.5 at ideal 69 is +1.5 on the curve (≈51%); turning off means the ideal is at least 73.5
        implied, offset = temporary_offset_for_change(0, MB_FALL, 70.5, 69.0)
        assert implied == pytest.approx(73.5) and offset == 3.0

    def test_full_speed_in_a_hot_room_says_nothing(self):
        assert temporary_offset_for_change(100, MB_FALL, 78.0, 72.0) is None

    def test_full_speed_in_a_mild_room_means_cooler(self):
        implied, offset = temporary_offset_for_change(100, MB_FALL, 72.0, 72.0)
        assert implied == pytest.approx(69.0) and offset == -3.0

    def test_inside_the_curve_is_unchanged(self):
        from auto_fan.comfort import offset_is_informative
        assert offset_is_informative(29, -0.5, MB_FALL)
        assert offset_is_informative(29, 1.0, MB_FALL)
        assert not offset_is_informative(6, -1.0, MB_FALL)   # at the floor, pointing cooler
        assert offset_is_informative(None, -1.0, MB_FALL)    # nothing to judge by

"""Tests for the override record (Phase 3)."""
from datetime import datetime
from unittest.mock import patch

from conftest import Device
from test_auto_fan_agent import _make_agent, _setup_zone_devices

FALL = patch("auto_fan.fan_zone.get_current_season", return_value="fall")


def _agent(fake_indigo, presence=True):
    _setup_zone_devices(fake_indigo, temp_value=75.0, fan_speed=0, presence=presence)
    agent = _make_agent()
    zone = agent.config.zones[0]
    zone.night_window["fall"] = {"start_hour": 20, "end_hour": 8}
    return agent, zone


def _records(agent):
    return list(agent.override_log.iter_records())


class TestOverrideRecord:
    def test_manual_change_writes_full_record(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        fan = fake_indigo.devices[100]
        fake_clock.set(datetime(2026, 10, 3, 22, 22))
        with FALL:
            agent.process_zone(zone)  # plugin commands ~60% (step 2 of 4)
            fake_clock.advance(seconds=30)  # past the self-change grace
            fan.speedIndex, fan.speedLevel = 3, 100  # person raises to high
            agent.process_device_change(fan, {"speedLevel": 100})
            agent.flush_settling_changes()  # the fan settled (normally 30 s later)
        recs = _records(agent)
        assert len(recs) == 1
        r = recs[0]
        assert r["type"] == "override" and r["zone"] == "Living Room"
        assert r["ts"] == "2026-10-03T22:22:30"
        assert r["season"] == "fall" and r["period"] == "night"
        assert r["plugin_step"] == 2 and r["human_step"] == 3 and r["human_pct"] == 100
        assert r["temps"] == {"Temp-200": 75.0} and r["temp_avg"] == 75.0 and r["ideal"] == 72.0
        assert r["presence"] is True and r["is_home"] is True
        assert r["lock_rule"] == "night_end" and r["lock_until"] == "2026-10-04T08:00:00"
        assert r["readjust"] is False and r["minutes_since_release"] is None

    def test_readjust_flagged(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        fan = fake_indigo.devices[100]
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            fan.speedIndex, fan.speedLevel = 2, 67
            agent.process_device_change(fan, {"speedLevel": 67})
            agent.flush_settling_changes()  # the fan settled (normally 30 s later)
            fake_clock.set(datetime(2026, 10, 3, 14, 20))
            fan.speedIndex, fan.speedLevel = 3, 100
            agent.process_device_change(fan, {"speedLevel": 100})
            agent.flush_settling_changes()  # the fan settled (normally 30 s later)
        recs = _records(agent)
        assert [r["readjust"] for r in recs] == [False, True]

    def test_release_recorded_with_resumed_speed_and_reoverride_noted(self, fake_indigo, fake_clock, speed_control_calls):
        agent, zone = _agent(fake_indigo, presence=False)
        fan = fake_indigo.devices[100]
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            fan.speedIndex, fan.speedLevel = 3, 100
            agent.process_device_change(fan, {"speedLevel": 100})
            agent.flush_settling_changes()  # the fan settled (normally 30 s later)
            fake_clock.set(datetime(2026, 10, 3, 15, 0, 3))
            agent.process_expired_lock(zone)          # unlock → plugin resumes ~60%
            fake_clock.set(datetime(2026, 10, 3, 15, 25, 3))
            fan.speedIndex, fan.speedLevel = 1, 33    # person changes it again within the hour
            agent.process_device_change(fan, {"speedLevel": 33})
            agent.flush_settling_changes()  # the fan settled (normally 30 s later)
        recs = _records(agent)
        assert [r["type"] for r in recs] == ["override", "release", "override"]
        rel = recs[1]
        assert rel["reason"] == "expired" and rel["lock_rule"] == "duration"
        # The manual change also derived a temporary comfort offset (ideal 72 →
        # 70), so automation resumes at the person's step rather than the old one.
        assert rel["lock_minutes"] == 60 and rel["resumed_step"] == 3
        assert recs[2]["minutes_since_release"] == 25.0

    def test_away_release_recorded(self, fake_indigo, fake_clock):
        from conftest import Variable
        agent, zone = _agent(fake_indigo)
        fake_indigo.variables[900] = Variable(900, name="home", value="true")
        agent.config.away_var_id = 900
        fake_clock.set(datetime(2026, 10, 3, 22, 0))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])
            fake_indigo.variables[900].value = "false"
            agent.process_variable_change(fake_indigo.variables[900], fake_indigo.variables[900])
        rel = [r for r in _records(agent) if r["type"] == "release"]
        assert len(rel) == 1 and rel[0]["reason"] == "house is away"

    def test_manual_reset_recorded(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])
            agent.reset_locks("Living Room")
        rel = [r for r in _records(agent) if r["type"] == "release"]
        assert rel[0]["reason"] == "manual reset"


class TestSummary:
    def test_summarize_groups_by_zone_season_period(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        fan = fake_indigo.devices[100]
        with FALL:
            for day in (1, 2, 3):
                fake_clock.set(datetime(2026, 10, day, 22, 0))
                agent.process_zone(zone)
                fake_clock.advance(seconds=30)
                fan.speedIndex, fan.speedLevel = 3, 100
                agent.process_device_change(fan, {"speedLevel": 100})
                agent.flush_settling_changes()  # the fan settled (normally 30 s later)
                agent.reset_locks("Living Room")
            fake_clock.set(datetime(2026, 10, 4, 10, 0))
            rows = agent.override_log.summarize(days=30)
        assert len(rows) == 1
        r = rows[0]
        assert (r["zone"], r["season"], r["period"]) == ("Living Room", "fall", "night")
        assert r["overrides"] == 3 and r["days"] == 3 and r["releases"] == 3
        assert r["mean_step_delta"] == 1.0

    def test_last_release_survives_restart(self, fake_indigo, fake_clock):
        from auto_fan.override_log import OverrideLog
        agent, zone = _agent(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])
            agent.reset_locks("Living Room")
        fresh = OverrideLog(agent.config.state_store)
        assert fresh._last_release["Living Room"] == datetime(2026, 10, 3, 14, 0)


class TestManualChangeSettles:
    def test_one_adjustment_reported_in_pieces_is_one_record(self, fake_indigo, fake_clock, monkeypatch):
        import time
        from auto_fan import auto_fan_agent
        monkeypatch.setattr(auto_fan_agent, "MANUAL_SETTLE_SECONDS", 0.05)
        agent, zone = _agent(fake_indigo)
        fan = fake_indigo.devices[100]
        fake_clock.set(datetime(2026, 10, 3, 22, 0))
        with FALL:
            agent.process_zone(zone)  # plugin commands step 2
            fake_clock.advance(seconds=30)
            # the fan reports one adjustment in pieces: off, then step 1, then step 3
            for idx, lvl in ((0, 0), (1, 33), (3, 100)):
                fan.speedIndex, fan.speedLevel = idx, lvl
                agent.process_device_change(fan, {"speedLevel": lvl})
            assert zone.locked is True               # locked at the first report
            assert _records(agent) == []             # recorded only once the fan settles
            assert zone.temporary_offset is None
            time.sleep(0.3)                          # the settle timer fires
        recs = _records(agent)
        assert len(recs) == 1
        assert recs[0]["human_step"] == 3 and recs[0]["human_pct"] == 100
        assert recs[0]["readjust"] is False          # the adjustment began as a fresh change
        assert zone.temporary_offset is not None
        agent.shutdown()

    def test_lock_released_before_the_fan_settles_still_records_the_change(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        fan = fake_indigo.devices[100]
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            fan.speedIndex, fan.speedLevel = 3, 100
            agent.process_device_change(fan, {"speedLevel": 100})
            agent.reset_locks("Living Room")
        assert [r["type"] for r in _records(agent)] == ["override", "release"]
        assert "Living Room" not in agent._settle_timers
        agent.shutdown()

    def test_shutdown_records_a_change_still_settling(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        fan = fake_indigo.devices[100]
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            fan.speedIndex, fan.speedLevel = 3, 100
            agent.process_device_change(fan, {"speedLevel": 100})
            agent.shutdown()
        assert [r["type"] for r in _records(agent)] == ["override"]

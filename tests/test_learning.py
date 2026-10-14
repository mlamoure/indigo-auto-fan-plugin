"""Tests for comfort learning (Phase 5): standing offsets, recommendations,
apply/dismiss, hand-edit detection, the watch, and the lock-rule rule."""
import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from conftest import Device
from test_auto_fan_agent import _setup_zone_devices
from test_comfort import MB_FALL

FALL = patch("auto_fan.fan_zone.get_current_season", return_value="fall")


def _fall():
    """A fresh patcher each time (the shared FALL object cannot be nested)."""
    return patch("auto_fan.fan_zone.get_current_season", return_value="fall")


def _agent_with_file(fake_indigo, tmp_path, zone_extra=None):
    """Agent whose config file stays on disk (apply_recommendation writes it)."""
    from auto_fan.auto_fan_config import AutoFanConfig
    from auto_fan.auto_fan_agent import AutoFanAgent
    _setup_zone_devices(fake_indigo, temp_value=72.1, fan_speed=0)
    zone_d = {"name": "Living Room", "fan_dev_id": 100, "temp_sensor_dev_ids": [200], "presence_dev_ids": [300],
              "night_window": {"fall": {"start_hour": 20, "end_hour": 8}},
              "seasonal_ideal_temp": {s: {"source": "static", "value": 72.0, "var_id": None} for s in ("spring", "summer", "fall", "winter")},
              "seasonal_curves": {s: {"temperature_range": 3, "num_points": 7, "points": [dict(p) for p in MB_FALL]} for s in ("spring", "summer", "fall", "winter")},
              "modifiers": {}}
    zone_d.update(zone_extra or {})
    conf = {"plugin_config": {"default_lock_duration": 60, "default_lock_extension_duration": 30}, "zones": [zone_d]}
    path = tmp_path / "auto_fan_conf.json"
    path.write_text(json.dumps(conf))
    config = AutoFanConfig(str(path), state_dir=str(tmp_path / "state"))
    agent = AutoFanAgent(config)
    return agent, agent.config.zones[0]


def _override(agent, zone, when, implied_offset, step=3):
    """Write one override record at ``when`` (fall, night when hour in window)."""
    with _fall():
        with patch("auto_fan.clock.now", return_value=when):
            agent.override_log.record_override(
                zone, "Fan", False, plugin_step=1, plugin_pct=33, human_step=step, human_pct=100,
                implied_ideal=72.0 + implied_offset, implied_offset=implied_offset)


def _release(agent, zone, when, reason="expired"):
    with _fall():
        with patch("auto_fan.clock.now", return_value=when):
            agent.override_log.record_release(zone, reason, "duration", when - timedelta(hours=1), 33, 1)


class TestStandingOffset:
    def test_loaded_and_applied_to_ideal(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path, {"comfort_offset": {"fall": {"day": 0, "night": -2.0}}})
        fake_clock.set(datetime(2026, 10, 3, 23, 0))
        with FALL:
            assert zone.standing_offset_value() == -2.0
            assert zone.get_ideal_temperature() == 70.0
            fake_clock.set(datetime(2026, 10, 3, 14, 0))
            assert zone.standing_offset_value() == 0.0 and zone.get_ideal_temperature() == 72.0
        agent.shutdown()

    def test_temporary_offset_is_relative_to_standing(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path, {"comfort_offset": {"fall": {"day": 0, "night": -2.0}}})
        fake_indigo.devices[200].states["sensorValue"] = 70.1  # room now matches the standing ideal
        fake_clock.set(datetime(2026, 10, 3, 23, 0))
        with FALL:
            implied_ideal, offset, _ = zone.derive_temporary_offset(57)  # 57% → Δ +2.1 → implied 68.0
            assert offset == -2.0 and round(implied_ideal, 1) == 68.0
            assert zone.standing_offset_value() == -2.0
        agent.shutdown()


class TestRecommendations:
    def _seed_night_overrides(self, agent, zone, now, offsets=(-2.0, -2.5, -2.0), days=(1, 2, 3)):
        for d, off in zip(days, offsets):
            _override(agent, zone, datetime(2026, 9, 20 + d, 22, 30), off)

    def test_observe_period_blocks_recommendations(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        now = datetime(2026, 9, 25, 10, 0)
        self._seed_night_overrides(agent, zone, now)
        assert agent.recommender.run(now) == []
        agent.shutdown()

    def test_recommends_standing_offset_from_evidence(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        now = datetime(2026, 10, 4, 10, 0)
        self._seed_night_overrides(agent, zone, now)
        with FALL:
            new = agent.recommender.run(now)
        assert len(new) == 1
        rec = new[0]
        assert rec["setting"] == "comfort_offset.fall.night" and rec["proposed"] == -2.0 and rec["current"] == 0.0
        assert rec["evidence"]["overrides"] == 3 and rec["evidence"]["days"] == 3
        assert "cooler" in rec["title"] and rec["status"] == "open"
        with FALL:
            assert agent.recommender.run(now) == []  # one open per setting
            assert agent.zone_status(zone)["open_recommendations"] == 1
        agent.shutdown()

    def test_needs_three_distinct_days(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        self._seed_night_overrides(agent, zone, None, days=(1, 1, 1))
        _override(agent, zone, datetime(2026, 9, 10, 22, 0), -1.0)  # makes the record old enough
        with FALL:
            new = agent.recommender.run(datetime(2026, 10, 4, 10, 0))
        assert new == []
        agent.shutdown()

    def test_learning_off_for_zone(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path, {"learning_mode": "off"})
        self._seed_night_overrides(agent, zone, None)
        with FALL:
            assert agent.recommender.run(datetime(2026, 10, 4, 10, 0)) == []
        agent.shutdown()

    def test_apply_writes_config_marks_applied_and_reloads(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        fake_clock.set(datetime(2026, 10, 4, 10, 0))
        self._seed_night_overrides(agent, zone, None)
        reloaded = []
        agent.reload_config_callback = lambda: reloaded.append(True)
        with FALL:
            rec = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
            ok, msg = agent.apply_recommendation(rec["id"])
        assert ok and "Applied" in msg
        saved = json.loads((tmp_path / "auto_fan_conf.json").read_text())
        assert saved["zones"][0]["comfort_offset"]["fall"]["night"] == -2.0
        assert any(p.name.startswith("auto_backup_") for p in (tmp_path / "auto_backups").iterdir())
        assert reloaded == [True]
        items = agent.recommender.recommendations()
        assert items[0]["status"] == "applied" and items[0]["watch_until"] == "2026-10-18T10:00:00"
        assert agent.recommender._changed_recently("Living Room", "comfort_offset.fall.night", datetime(2026, 10, 6))
        # pace: no new recommendation for that setting within 7 days
        with FALL:
            assert agent.recommender.run(datetime(2026, 10, 6, 10, 0)) == []
        agent.shutdown()

    def test_dismiss(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        self._seed_night_overrides(agent, zone, None)
        with FALL:
            rec = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
            ok, msg = agent.dismiss_recommendation(rec["id"])
            assert ok and agent.recommender.recommendations()[0]["status"] == "dismissed"
            # dismissed → may be proposed again later (not blocked by pace)
            assert len(agent.recommender.run(datetime(2026, 10, 4, 11, 0))) == 1
        agent.shutdown()

    def test_hand_edit_dismisses_and_restarts_pace(self, fake_indigo, tmp_path, fake_clock):
        from auto_fan.auto_fan_agent import AutoFanAgent
        from auto_fan.auto_fan_config import AutoFanConfig
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        self._seed_night_overrides(agent, zone, None)
        fake_clock.set(datetime(2026, 10, 4, 10, 0))
        with FALL:
            rec = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
        agent.shutdown()
        # Mike edits the fall night offset by hand in the web editor → config saved → reload
        conf = json.loads((tmp_path / "auto_fan_conf.json").read_text())
        conf["zones"][0]["comfort_offset"] = {"fall": {"day": 0, "night": -1.0}}
        (tmp_path / "auto_fan_conf.json").write_text(json.dumps(conf))
        with FALL:
            config2 = AutoFanConfig(str(tmp_path / "auto_fan_conf.json"), state_dir=str(tmp_path / "state"))
            agent2 = AutoFanAgent(config2)
        items = agent2.recommender.recommendations()
        assert items[0]["id"] == rec["id"] and items[0]["status"] == "dismissed"
        assert agent2.recommender._changed_recently("Living Room", "comfort_offset.fall.night", datetime(2026, 10, 5))
        agent2.shutdown()

    def test_watch_confirms_or_reverts(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        self._seed_night_overrides(agent, zone, None)
        agent.reload_config_callback = lambda: None
        fake_clock.set(datetime(2026, 10, 4, 10, 0))
        with FALL:
            rec = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
            agent.apply_recommendation(rec["id"])
            # overrides keep coming after the change → watch fails → revert recommended
            for d in (6, 8, 10, 12):
                _override(agent, zone, datetime(2026, 10, d, 22, 30), -2.0)
            new = agent.recommender.run(datetime(2026, 10, 19, 10, 0))
        items = agent.recommender.recommendations()
        assert items[0]["status"] == "watch_failed" and items[0]["watch_result"] == {"before": 3, "after": 4}
        revert = [r for r in new if r.get("revert_of") == rec["id"]]
        assert len(revert) == 1 and revert[0]["proposed"] == 0.0 and revert[0]["current"] == -2.0
        agent.shutdown()

    def test_watch_confirms_when_overrides_fall(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        self._seed_night_overrides(agent, zone, None)
        agent.reload_config_callback = lambda: None
        fake_clock.set(datetime(2026, 10, 4, 10, 0))
        with FALL:
            rec = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
            agent.apply_recommendation(rec["id"])
            new = agent.recommender.run(datetime(2026, 10, 19, 10, 0))
        assert agent.recommender.recommendations()[0]["status"] == "confirmed"
        assert not [r for r in new if r.get("revert_of")]
        agent.shutdown()


class TestLockRuleRecommendation:
    def test_reoverrides_after_releases_recommend_night_hold(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path, {"hold_locks_at_night": "off"})
        _override(agent, zone, datetime(2026, 9, 1, 22, 0), -1.0)  # old enough record
        for d in range(1, 7):
            when = datetime(2026, 9, 20 + d, 2, 0)
            _release(agent, zone, when)
            if d <= 4:  # 4 of 6 releases re-overridden within the hour, at night
                with _fall():
                    with patch("auto_fan.clock.now", return_value=when + timedelta(minutes=20)):
                        agent.override_log.record_override(zone, "Fan", False, 1, 33, 3, 100, 70.0, -2.0)
        with FALL:
            new = agent.recommender.run(datetime(2026, 10, 4, 10, 0))
        rule = [r for r in new if r["setting"] == "hold_locks_at_night"]
        assert len(rule) == 1 and rule[0]["proposed"] == "on" and rule[0]["evidence"]["reoverrides"] == 4
        agent.shutdown()

    def test_apply_lock_rule(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path, {"hold_locks_at_night": "off"})
        agent.reload_config_callback = lambda: None
        _override(agent, zone, datetime(2026, 9, 1, 22, 0), -1.0)
        for d in range(1, 7):
            when = datetime(2026, 9, 20 + d, 2, 0)
            _release(agent, zone, when)
            with _fall():
                with patch("auto_fan.clock.now", return_value=when + timedelta(minutes=20)):
                    agent.override_log.record_override(zone, "Fan", False, 1, 33, 3, 100, 70.0, -2.0)
        with FALL:
            rec = [r for r in agent.recommender.run(datetime(2026, 10, 4, 10, 0)) if r["setting"] == "hold_locks_at_night"][0]
            ok, _ = agent.apply_recommendation(rec["id"])
        assert ok
        assert json.loads((tmp_path / "auto_fan_conf.json").read_text())["zones"][0]["hold_locks_at_night"] == "on"
        agent.shutdown()


class TestEvidenceCountsNights:
    def test_a_night_across_midnight_is_one_night(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        _override(agent, zone, datetime(2026, 9, 21, 22, 30), -2.0)
        _override(agent, zone, datetime(2026, 9, 22, 3, 0), -2.0)   # the same night, after midnight
        _override(agent, zone, datetime(2026, 9, 23, 22, 30), -2.0)
        with FALL:
            assert agent.recommender.run(datetime(2026, 10, 4, 10, 0)) == []  # two nights, not three
            _override(agent, zone, datetime(2026, 9, 25, 23, 0), -2.0)
            new = agent.recommender.run(datetime(2026, 10, 4, 10, 0))
        assert len(new) == 1
        assert new[0]["evidence"]["overrides"] == 4 and new[0]["evidence"]["days"] == 3
        assert "4 time(s) on 3 different night(s)" in new[0]["text"]
        agent.shutdown()

    def test_each_night_weighs_the_same(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        for minute in (0, 10, 20, 30, 40):   # one restless night: five changes asking for -3
            _override(agent, zone, datetime(2026, 9, 21, 22, minute), -3.0)
        for d in (23, 25, 27):                # three calm nights asking for -1
            _override(agent, zone, datetime(2026, 9, d, 22, 30), -1.0)
        with FALL:
            new = agent.recommender.run(datetime(2026, 10, 4, 10, 0))
        assert new[0]["proposed"] == -1.0
        agent.shutdown()

    def test_watch_counts_nights(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        for d in (21, 22, 23):
            _override(agent, zone, datetime(2026, 9, d, 22, 30), -2.0)
        agent.reload_config_callback = lambda: None
        fake_clock.set(datetime(2026, 10, 4, 10, 0))
        with FALL:
            rec = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
            agent.apply_recommendation(rec["id"])
            _override(agent, zone, datetime(2026, 10, 6, 22, 30), -1.0)  # one night after the change,
            _override(agent, zone, datetime(2026, 10, 7, 2, 0), -1.0)    # twice
            agent.recommender.run(datetime(2026, 10, 19, 10, 0))
        item = agent.recommender.recommendations()[0]
        assert item["status"] == "confirmed" and item["watch_result"] == {"before": 3, "after": 1}
        agent.shutdown()


class TestLearningSchedule:
    def test_a_run_never_rearms_for_the_same_morning(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        fake_clock.set(datetime(2026, 10, 13, 10, 0, 28))   # the timer fired 2 s early by the wall clock
        agent._schedule_learning_run(after_run=True)
        assert agent._learning_timer.interval == pytest.approx(86402, abs=1)
        agent._schedule_learning_run()                       # a plugin start at that moment still runs today
        assert agent._learning_timer.interval == pytest.approx(2, abs=1)
        agent.shutdown()

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
        # 3 of 13 counted nights before (the record began 9/21), 4 of 14 after
        assert items[0]["status"] == "watch_failed" and items[0]["watch_result"] == {
            "before": 3, "after": 4, "before_days": 13, "after_days": 14}
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
        assert item["status"] == "confirmed" and item["watch_result"] == {
            "before": 3, "after": 1, "before_days": 13, "after_days": 14}
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


def _night_change(agent, zone, when, plugin_step, human_step, human_pct, implied_offset):
    """One override with explicit steps (a 7-step BAF-like fan: step × 100/7 %)."""
    with _fall():
        with patch("auto_fan.clock.now", return_value=when):
            agent.override_log.record_override(
                zone, "Fan", False, plugin_step=plugin_step, plugin_pct=plugin_step * 100 / 7,
                human_step=human_step, human_pct=human_pct,
                implied_ideal=72.0 + implied_offset, implied_offset=implied_offset)


class TestCurveEndEvidence:
    def test_turning_off_in_a_cold_room_is_not_evidence_for_cooler(self, fake_indigo, tmp_path, fake_clock):
        # Old records written before the fix: off (0%) with a "cooler" offset read off the curve's flat end
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        _night_change(agent, zone, datetime(2026, 9, 21, 22, 0), 1, 0, 0, -3.0)
        _night_change(agent, zone, datetime(2026, 9, 23, 22, 0), 1, 0, 0, -3.0)
        _night_change(agent, zone, datetime(2026, 9, 25, 22, 0), 1, 2, 29, -1.0)
        with FALL:
            assert agent.recommender.run(datetime(2026, 10, 4, 10, 0)) == []
        agent.shutdown()


class TestNewerEvidence:
    def _seed(self, agent, zone, offsets, days=(21, 23, 25)):
        for d, off in zip(days, offsets):
            _override(agent, zone, datetime(2026, 9, d, 22, 30), off)

    def test_newer_evidence_replaces_an_open_recommendation(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        self._seed(agent, zone, (-2.0, -2.0, -2.0))
        with FALL:
            first = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
            assert first["proposed"] == -2.0
            # four more nights asking for -1: the median moves
            self._seed(agent, zone, (-1.0, -1.0, -1.0, -1.0), days=(27, 28, 29, 30))
            new = agent.recommender.run(datetime(2026, 10, 5, 10, 0))
        assert len(new) == 1 and new[0]["proposed"] == -1.0
        old = next(r for r in agent.recommender.recommendations() if r["id"] == first["id"])
        assert old["status"] == "superseded" and old["superseded_by"] == new[0]["id"]
        assert [r["id"] for r in agent.recommender.open_for_zone(zone.name)] == [new[0]["id"]]
        agent.shutdown()

    def test_same_proposal_keeps_the_open_one_and_refreshes_it(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        self._seed(agent, zone, (-2.0, -2.0, -2.0))
        with FALL:
            first = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
            _override(agent, zone, datetime(2026, 9, 27, 22, 30), -2.0)
            assert agent.recommender.run(datetime(2026, 10, 5, 10, 0)) == []
        kept = agent.recommender.open_for_zone(zone.name)
        assert len(kept) == 1 and kept[0]["id"] == first["id"] and kept[0]["evidence"]["days"] == 4
        agent.shutdown()

    def test_withdrawn_when_the_evidence_ages_out(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        self._seed(agent, zone, (-2.0, -2.0, -2.0))
        with FALL:
            first = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
            agent.recommender.run(datetime(2026, 11, 30, 10, 0))   # 60-day window has passed
        old = agent.recommender.recommendations()[0]
        assert old["id"] == first["id"] and old["status"] == "withdrawn"
        assert agent.recommender.open_for_zone(zone.name) == []
        agent.shutdown()

    def test_open_revert_wins_over_an_older_open_recommendation(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        store = agent.recommender.store
        base = {"zone": zone.name, "setting": "comfort_offset.fall.night", "status": "open",
                "bucket": {"season": "fall", "period": "night"}, "title": "t", "text": "x", "evidence": {},
                "setting_label": "Fall night comfort offset", "current_label": "-2.0°F"}
        store.save("recommendations", {"items": [
            dict(base, id="older", created="2026-10-21T08:00:00", current=-2.0, proposed=-1.5, proposed_label="-1.5°F"),
            dict(base, id="revert", created="2026-10-28T08:00:00", current=-2.0, proposed=0.0, proposed_label="+0.0°F",
                 revert_of="applied1"),
        ]})
        self._seed(agent, zone, (-1.0, -1.0, -1.0))
        with FALL:
            assert agent.recommender.run(datetime(2026, 10, 4, 10, 0)) == []
        by_id = {r["id"]: r for r in agent.recommender.recommendations()}
        assert by_id["older"]["status"] == "superseded" and by_id["revert"]["status"] == "open"
        agent.shutdown()

    def test_warmer_text_counts_the_warmer_changes(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        for d in (21, 23, 25):  # a mid-curve speed, so a warmer offset counts
            _night_change(agent, zone, datetime(2026, 9, d, 14, 0), 3, 2, 29, 1.5)
        with FALL:
            rec = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
        assert "warmer" in rec["title"] and "3 of them asked for a warmer room" in rec["text"]
        agent.shutdown()


class TestNightLimits:
    def _swinging_nights(self, agent, zone):
        # cold nights: automation at step 1, people raise it to 2–3; warm nights: automation at 5, people lower to 3–4
        for d, (auto, human, pct, off) in zip(range(21, 29), [
                (1, 2, 29, -1.0), (1, 2, 29, -1.0), (1, 3, 43, -2.0), (1, 2, 29, -1.0),
                (5, 4, 57, 1.0), (5, 3, 43, 2.0), (1, 2, 29, -1.0), (5, 4, 57, 1.0)]):
            _night_change(agent, zone, datetime(2026, 9, d, 22, 0), auto, human, pct, off)

    def test_swinging_nights_recommend_limits_not_an_offset(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        self._swinging_nights(agent, zone)
        with FALL:
            new = agent.recommender.run(datetime(2026, 10, 4, 10, 0))
        assert [r["setting"] for r in new] == ["night_limits.fall"]
        rec = new[0]
        assert rec["current"] == [0, 100] and rec["proposed"] == [30, 60]
        assert rec["proposed_label"] == "min 30%, max 60%" and rec["current_label"] == "min none, max none"
        assert rec["evidence"]["raised"] == 5 and rec["evidence"]["lowered"] == 3
        assert agent.recommender.open_for_zone(zone.name)[0]["section"] == "nighttime"
        agent.shutdown()

    def test_a_readjustment_without_a_plugin_step_still_ends_the_night(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        self._swinging_nights(agent, zone)
        with _fall():
            with patch("auto_fan.clock.now", return_value=datetime(2026, 9, 21, 23, 30)):
                agent.override_log.record_override(zone, "Fan", True, plugin_step=None, plugin_pct=0,
                                                   human_step=1, human_pct=14, implied_ideal=None, implied_offset=None)
        swing = agent.recommender._night_swing(zone, "fall", list(agent.override_log.iter_records()),
                                               datetime(2026, 8, 1))
        # 9/21 now ends where it began (step 1, 14%): counted, but neither up nor down
        assert swing["nights"] == 8 and swing["raised"] == 4 and swing["lowered"] == 3 and swing["low_pct"] == 14
        agent.shutdown()

    def test_one_way_nights_still_get_an_offset(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        for d in range(21, 27):
            _night_change(agent, zone, datetime(2026, 9, d, 22, 0), 1, 2, 29, -1.0)
        with FALL:
            new = agent.recommender.run(datetime(2026, 10, 4, 10, 0))
        assert [r["setting"] for r in new] == ["comfort_offset.fall.night"]
        agent.shutdown()

    def test_limits_replace_an_open_offset_recommendation(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        for d in (21, 22, 23):
            _night_change(agent, zone, datetime(2026, 9, d, 22, 0), 1, 2, 29, -1.0)
        with FALL:
            offset_rec = agent.recommender.run(datetime(2026, 10, 1, 10, 0))[0]
            for d in (24, 25, 26):
                _night_change(agent, zone, datetime(2026, 9, d, 22, 0), 5, 4, 57, 1.0)
            agent.recommender.run(datetime(2026, 10, 2, 10, 0))
        old = next(r for r in agent.recommender.recommendations() if r["id"] == offset_rec["id"])
        assert old["status"] == "withdrawn" and "night speed limits" in old["withdrawn_reason"]
        assert [r["setting"] for r in agent.recommender.open_for_zone(zone.name)] == ["night_limits.fall"]
        agent.shutdown()

    def test_apply_writes_the_nighttime_modifier(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path, {"modifiers": {"nighttime": {"fall": {"clamp_min_pct": 20, "clamp_max_pct": 100}}}})
        agent.reload_config_callback = lambda: None
        self._swinging_nights(agent, zone)
        fake_clock.set(datetime(2026, 10, 4, 10, 0))
        with FALL:
            rec = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
            assert rec["current"] == [20, 100]
            ok, _ = agent.apply_recommendation(rec["id"])
        assert ok
        saved = json.loads((tmp_path / "auto_fan_conf.json").read_text())
        assert saved["zones"][0]["modifiers"]["nighttime"]["fall"] == {"clamp_min_pct": 30, "clamp_max_pct": 60}
        assert agent.recommender.store.load("learnable_snapshot")["Living Room"]["night_limits.fall"] == [30, 60]
        agent.shutdown()


class TestWatchCountsHomeDays:
    def test_away_days_do_not_count(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        for d in (21, 22, 23):
            _override(agent, zone, datetime(2026, 9, d, 22, 30), -2.0)
        agent.reload_config_callback = lambda: None
        # presence tracked from 9/20; home only 9/20–9/26, then every day from the change on
        rec_store = agent.recommender
        for d in range(20, 27):
            rec_store._home_noted = None
            rec_store.note_home(datetime(2026, 9, d, 12, 0), True)
        for d in range(4, 19):
            rec_store._home_noted = None
            rec_store.note_home(datetime(2026, 10, d, 12, 0), True)
        fake_clock.set(datetime(2026, 10, 4, 10, 0))
        with FALL:
            rec = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
            agent.apply_recommendation(rec["id"])
            for d in (6, 9, 12):  # 3 of 14 home nights after
                _override(agent, zone, datetime(2026, 10, d, 22, 30), -2.0)
            agent.recommender.run(datetime(2026, 10, 19, 10, 0))
        item = agent.recommender.recommendations()[0]
        # before: 3 of 6 home nights (9/21–9/26); after: 3 of 14 → the share fell
        assert item["watch_result"] == {"before": 3, "after": 3, "before_days": 6, "after_days": 14}
        assert item["status"] == "confirmed"
        agent.shutdown()

    def test_no_counted_days_is_inconclusive(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        for d in (21, 22, 23):
            _override(agent, zone, datetime(2026, 9, d, 22, 30), -2.0)
        agent.reload_config_callback = lambda: None
        agent.recommender.note_home(datetime(2026, 9, 1, 12, 0), False)  # tracking began; away ever since
        fake_clock.set(datetime(2026, 10, 4, 10, 0))
        with FALL:
            rec = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
            agent.apply_recommendation(rec["id"])
            new = agent.recommender.run(datetime(2026, 10, 19, 10, 0))
        assert agent.recommender.recommendations()[0]["status"] == "watch_inconclusive"
        assert not [r for r in new if r.get("revert_of")]
        agent.shutdown()

    def test_note_home_writes_once_a_day(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        r = agent.recommender
        r.note_home(datetime(2026, 10, 4, 9, 0), True)
        with patch.object(r.store, "save") as save:
            r.note_home(datetime(2026, 10, 4, 21, 0), True)
            save.assert_not_called()
        r.note_home(datetime(2026, 10, 5, 9, 0), True)
        assert r.store.load("home_days") == {"since": "2026-10-04", "dates": ["2026-10-04", "2026-10-05"]}
        agent.shutdown()


class TestAdjustmentSessions:
    def test_changes_minutes_apart_count_where_they_ended(self, fake_indigo, tmp_path, fake_clock):
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        for d in (21, 23, 25):
            # set too far (asks for -3), corrected ten minutes later (asks for -1): one decision, -1
            _override(agent, zone, datetime(2026, 9, d, 22, 0), -3.0)
            _override(agent, zone, datetime(2026, 9, d, 22, 10), -1.0)
        with FALL:
            rec = agent.recommender.run(datetime(2026, 10, 4, 10, 0))[0]
        assert rec["proposed"] == -1.0
        assert rec["evidence"]["overrides"] == 3 and rec["evidence"]["days"] == 3
        agent.shutdown()

    def test_changes_hours_apart_stay_separate(self, fake_indigo, tmp_path, fake_clock):
        from auto_fan.learning import settled_changes
        agent, zone = _agent_with_file(fake_indigo, tmp_path)
        _override(agent, zone, datetime(2026, 9, 21, 22, 0), -1.0)
        _override(agent, zone, datetime(2026, 9, 21, 22, 14), -2.0)   # 14 min later: same decision
        _override(agent, zone, datetime(2026, 9, 21, 22, 29), -2.5)   # chained, 15 min after: a new one
        _override(agent, zone, datetime(2026, 9, 22, 4, 30), 1.0)     # middle of the night: separate
        kept = settled_changes(list(agent.override_log.iter_records()), zone.name)
        assert [r["ts"][11:16] for r in kept] == ["22:14", "22:29", "04:30"]
        agent.shutdown()

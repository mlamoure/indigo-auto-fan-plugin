"""Web editor zone status panel (needs WTForms/Jinja2; skipped without them)."""
import json
import os
import re
import tempfile
from datetime import datetime
from unittest.mock import patch

import pytest

pytest.importorskip("wtforms")

from test_comfort import _agent, FALL


def _handler(agent):
    from config_web_editor.config_editor import WebConfigEditor
    from config_web_editor.iws_web_handler import IWSWebHandler
    base = os.path.join(os.path.dirname(__file__), os.pardir, "Auto Fan.indigoPlugin", "Contents", "Server Plugin")
    schema = os.path.join(base, "config_web_editor", "config", "config_schema.json")
    tmpdir = tempfile.mkdtemp()
    conf = os.path.join(tmpdir, "auto_fan_conf.json")
    with open(conf, "w") as f:
        json.dump({"plugin_config": {}, "zones": [{"name": "Living Room", "fan_dev_id": 100, "temp_sensor_dev_ids": [200],
                                                    "zone_index": 0, "night_window": {}, "modifiers": {}}]}, f)
    editor = WebConfigEditor(conf, schema, os.path.join(tmpdir, "b"), os.path.join(tmpdir, "ab"))
    editor._indigo_devices_cache["data"] = [{"id": 100, "name": "Fan", "class": "indigo.SpeedControlDevice"},
                                            {"id": 200, "name": "Temp", "class": "indigo.SensorDevice"}]
    editor._indigo_variables_cache["data"] = []
    return IWSWebHandler(editor, "com.vtmikel.autofan", agent_provider=lambda: agent)


class TestZonePageLayout:
    def test_strip_and_offset_render_above_the_form(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 22, 0))
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])   # locks, derives an offset
            agent.set_comfort_offset("Living Room", -2.0, until="night_end", source="web page")  # hand-set wins
            h = _handler(agent)
            reply = h.handle_request("GET", {}, {}, {"page": "zone/0"})
        html = reply["content"]
        assert reply["status"] == 200
        assert "Living Room — right now" in html
        assert "🔒 until Sun 08:00 (night window)" in html
        assert 'name="action" value="clear_lock"' in html
        assert "-2.0° until Sun 08:00 (web page)" in html
        # the strip sits above the settings form; the old bottom panel is gone
        assert html.index('id="zone-now"') < html.index('id="zone-config-form"')
        assert "zone-status-panel" not in html and "Current Status" not in html
        # no Set button: the right-now controls apply on change
        assert 'id="now-offset-select"' in html and ">Set<" not in html

    def test_json_set_and_clear_offset(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 14, 0))
        with FALL:
            h = _handler(agent)
            reply = h.handle_request("POST", {}, {"format": "json", "action": "set_offset", "offset": "-1.5", "until": "hours", "hours": "2"},
                                     {"page": "zone_status/0"})
            data = json.loads(reply["content"])
            assert reply["headers"]["Content-Type"] == "application/json"
            assert data["ok"] is True and data["status"]["temporary_offset"] == -1.5
            assert data["status"]["temporary_offset_expires"] == "Sat 16:00"
            assert zone.temporary_offset_expires == datetime(2026, 10, 3, 16, 0)
            reply = h.handle_request("POST", {}, {"format": "json", "action": "clear_offset"}, {"page": "zone_status/0"})
            data = json.loads(reply["content"])
        assert data["ok"] is True and data["status"]["temporary_offset"] is None
        assert zone.temporary_offset is None

    def test_post_clear_lock_rerenders_page(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        with FALL:
            agent.on_manual_change(zone, fake_indigo.devices[100])
            h = _handler(agent)
            reply = h.handle_request("POST", {}, {"action": "clear_lock"}, {"page": "zone_status/0"})
        assert zone.locked is False
        assert "Lock cleared" in reply["content"]

    def test_comfort_recommendation_sits_in_comfort_offset_section(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        agent.recommender._save_recommendations([{
            "id": "abc123", "zone": "Living Room", "setting": "comfort_offset.fall.night",
            "setting_label": "Fall night comfort offset", "title": "Living Room: people want it cooler on fall nights",
            "text": "In the last 60 days the fan was changed by hand 9 times on 6 days.", "current": 0.0, "proposed": -2.5,
            "current_label": "+0.0°F", "proposed_label": "-2.5°F", "evidence": {}, "status": "open",
            "created": "2026-10-04T10:00:00", "bucket": {"season": "fall", "period": "night"}}])
        with FALL:
            h = _handler(agent)
            html = h.handle_request("GET", {}, {}, {"page": "zone/0"})["content"]
            zones_html = h.handle_request("GET", {}, {}, {"page": "zones"})["content"]
        assert "💡 Recommendations (1)" in html
        assert "people want it cooler on fall nights" in html
        # the card is inside the Comfort Offset section, not above the form
        card = html.index('id="comfort-recommendations"')
        assert html.index("<legend>Comfort Offset</legend>") < card < html.index("<legend>Fan Speed Curve</legend>")
        assert 'id="lock-recommendations"' not in html
        # its buttons post a small form outside the settings form (forms cannot nest)
        assert html.index('id="rec-form-abc123"') < html.index('id="zone-config-form"')
        assert 'form="rec-form-abc123" name="action" value="apply_recommendation"' in html
        assert 'form="rec-form-abc123" name="action" value="dismiss_recommendation"' in html
        # the right-now strip points at it
        assert 'href="#comfort-recommendations"' in html and 'href="#lock-recommendations"' not in html
        # the feature is labelled beta on the card and in the strip
        assert '<span class="beta-tag">beta</span>' in html and "open recommendation (beta)" in html
        assert "💡 1 open" in zones_html

    def test_lock_recommendation_sits_in_lock_behavior_section(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        agent.recommender._save_recommendations([{
            "id": "lock01", "zone": "Living Room", "setting": "hold_locks_at_night", "setting_label": "Hold locks at nighttime",
            "title": "Living Room: hold locks at nighttime", "text": "e", "current": "off", "proposed": "on",
            "current_label": "off", "proposed_label": "on", "evidence": {}, "status": "open",
            "created": "2026-10-04T10:00:00", "bucket": None}])
        with FALL:
            h = _handler(agent)
            html = h.handle_request("GET", {}, {}, {"page": "zone/0"})["content"]
        card = html.index('id="lock-recommendations"')
        assert html.index("<legend>Lock Behavior</legend>") < card
        assert 'id="comfort-recommendations"' not in html
        assert 'href="#lock-recommendations"' in html and 'href="#comfort-recommendations"' not in html

    def test_comfort_learning_tooltip_explains_each_choice(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        with FALL:
            html = _handler(agent).handle_request("GET", {}, {}, {"page": "zone/0"})["content"]
        assert "Comfort Learning (beta)" in html and "Beta. Whether the plugin learns" in html
        assert "Use plugin default: follow the Comfort Learning setting" in html
        assert "Suggest: once a day" in html and "Off: no recommendations" in html
        # the dropdown shows the same words as the tooltip, not the stored values
        select = re.search(r'<select id="learning_mode".*?</select>', html, re.S).group(0)
        assert re.findall(r'value="(\w+)">([^<]+)</option>', select) == [
            ("inherit", "Use plugin default"), ("suggest", "Suggest"), ("off", "Off")]

    def test_dismiss_from_page(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        agent.recommender._save_recommendations([{
            "id": "abc123", "zone": "Living Room", "setting": "comfort_offset.fall.night", "setting_label": "x",
            "title": "t", "text": "e", "current": 0.0, "proposed": -2.5, "current_label": "0", "proposed_label": "-2.5",
            "evidence": {}, "status": "open", "created": "2026-10-04T10:00:00", "bucket": None}])
        with FALL:
            h = _handler(agent)
            reply = h.handle_request("POST", {}, {"action": "dismiss_recommendation", "rec_id": "abc123"}, {"page": "zone_status/0"})
        assert agent.recommender.recommendations()[0]["status"] == "dismissed"
        assert "Dismissed" in reply["content"]

    def test_zones_list_shows_badges(self, fake_indigo, fake_clock):
        agent, zone = _agent(fake_indigo)
        fake_clock.set(datetime(2026, 10, 3, 22, 0))
        with FALL:
            agent.set_comfort_offset("Living Room", -2.0, until="night_end")
            h = _handler(agent)
            html = h.handle_request("GET", {}, {}, {"page": "zones"})["content"]
        assert "🎚️ -2.0° until Sun 08:00" in html

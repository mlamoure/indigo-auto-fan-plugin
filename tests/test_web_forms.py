"""Web editor forms through WTForms: a browser-style save of the zone and plugin
pages coerces device, variable and choice values, the saved values render as
selected, and the select fields validate their choices."""
import json
import os
import shutil
import tempfile

import pytest
from werkzeug.datastructures import MultiDict

from config_web_editor.config_editor import WebConfigEditor
from config_web_editor.iws_form_helpers import generate_form_class_from_schema
from config_web_editor.iws_web_handler import IWSWebHandler

BASE = os.path.join(os.path.dirname(__file__), os.pardir, "Auto Fan.indigoPlugin", "Contents", "Server Plugin")
CONFIG_DIR = os.path.join(BASE, "config_web_editor", "config")

DEVICES = [
    {"id": 100, "name": "Fan", "class": "indigo.SpeedControlDevice"},
    {"id": 101, "name": "Other Fan", "class": "indigo.SpeedControlDevice"},
    {"id": 200, "name": "Temp A", "class": "indigo.SensorDevice"},
    {"id": 201, "name": "Temp B", "class": "indigo.SensorDevice"},
    {"id": 300, "name": "Thermostat", "class": "indigo.ThermostatDevice"},
]
VARIABLES = [{"id": 500, "name": "away"}, {"id": 501, "name": "season"}]


@pytest.fixture
def web(tmp_path):
    conf = tmp_path / "auto_fan_conf.json"
    shutil.copy(os.path.join(CONFIG_DIR, "auto_fan_conf_empty.json"), conf)
    editor = WebConfigEditor(str(conf), os.path.join(CONFIG_DIR, "config_schema.json"),
                             str(tmp_path / "backups"), str(tmp_path / "auto_backups"))
    editor._indigo_devices_cache["data"] = list(DEVICES)
    editor._indigo_variables_cache["data"] = list(VARIABLES)
    handler = IWSWebHandler(editor, "com.vtmikel.autofan", agent_provider=lambda: None)

    def request(method, page, body=None):
        reply = handler.handle_request(method, {}, body or {}, {"page": page})
        assert reply["status"] == 200, (method, page, reply["status"])
        return reply["content"]

    def saved():
        return json.loads(conf.read_text())

    return request, saved, editor


def test_zone_save_coerces_devices_and_renders_them_selected(web):
    request, saved, _ = web
    request("GET", "zone/new")
    # IWS hands a multi-value field over as a list, a single value as a string
    request("POST", "zone/new", {
        "name": "Den", "enabled": "y",
        "fan_dev_id": "100", "thermostat_dev_id": "300",
        "temp_sensor_dev_ids": ["200", "201"],
        "learning_mode": "off",
    })
    zone = saved()["zones"][0]
    assert zone["name"] == "Den"
    assert zone["fan_dev_id"] == 100
    assert zone["thermostat_dev_id"] == 300
    assert zone["temp_sensor_dev_ids"] == [200, 201]
    assert zone["learning_mode"] == "off"

    html = request("GET", "zone/0")
    assert '<option selected value="100">Fan</option>' in html
    assert '<option selected value="300">Thermostat</option>' in html
    assert '<option value="201" selected>Temp B</option>' in html
    assert '<option selected value="off">Off</option>' in html


def test_plugin_config_save_coerces_choices_and_renders_them_selected(web):
    request, saved, _ = web
    request("GET", "plugin_config")
    request("POST", "plugin_config", {
        "hemisphere": "south", "learning_mode": "off",
        "season_detection_mode": "automatic",
        "away_var_id": "500", "default_lock_duration": "45",
    })
    cfg = saved()["plugin_config"]
    assert cfg["hemisphere"] == "south"
    assert cfg["learning_mode"] == "off"
    assert cfg["away_var_id"] == 500
    assert cfg["default_lock_duration"] == 45

    html = request("GET", "plugin_config")
    assert '<option selected value="south">Southern Hemisphere</option>' in html
    assert '<option selected value="500">away</option>' in html


def _zone_form(editor, formdata):
    schema = editor.config_schema["properties"]["zones"]["items"]
    form = generate_form_class_from_schema(schema)(formdata=MultiDict(formdata))
    form.fan_dev_id.choices = [(-1, "None Selected")] + [(d["id"], d["name"]) for d in DEVICES[:2]]
    form.temp_sensor_dev_ids.choices = [(d["id"], d["name"]) for d in DEVICES[2:4]]
    return form


def test_zone_select_fields_validate_their_choices(web):
    _, _, editor = web
    form = _zone_form(editor, [("fan_dev_id", "101"), ("temp_sensor_dev_ids", "200"),
                               ("temp_sensor_dev_ids", "201"), ("learning_mode", "suggest")])
    assert form.fan_dev_id.data == 101
    assert form.temp_sensor_dev_ids.data == [200, 201]
    for field in (form.fan_dev_id, form.temp_sensor_dev_ids, form.learning_mode):
        assert field.validate(form), (field.name, field.errors)

    bad = _zone_form(editor, [("fan_dev_id", "999"), ("temp_sensor_dev_ids", "999"),
                              ("learning_mode", "sometimes")])
    for field in (bad.fan_dev_id, bad.temp_sensor_dev_ids, bad.learning_mode):
        assert not field.validate(bad), field.name
    assert "999" in bad.temp_sensor_dev_ids.errors[0]

    not_a_number = _zone_form(editor, [("fan_dev_id", "abc")])
    assert not not_a_number.fan_dev_id.validate(not_a_number)
    assert not_a_number.fan_dev_id.data is None

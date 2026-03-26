import sys
import types
import datetime
import os

# Stub indigo module before any plugin imports


class Devices(dict):
    def __iter__(self):
        return iter(self.values())

    def __missing__(self, key):
        dev = Device(key)
        self[key] = dev
        return dev

    def subscribeToChanges(self):
        pass


class Variables(dict):
    def __iter__(self):
        return iter(self.values())

    def __missing__(self, key):
        var = Variable(key)
        self[key] = var
        return var

    def subscribeToChanges(self):
        pass


class Device:
    def __init__(self, id, name="", onState=False, brightness=0, sensorValue=None,
                 speedLevel=None, heatSetpoint=None, coolSetpoint=None, hvacMode=None):
        self.id = id
        self.name = name or f"Dev-{id}"
        self.onState = onState
        self.onOffState = onState
        self.brightness = brightness
        self.sensorValue = sensorValue if sensorValue is not None else brightness
        self.speedLevel = speedLevel
        self.heatSetpoint = heatSetpoint
        self.coolSetpoint = coolSetpoint
        self.hvacMode = hvacMode
        self.states = {
            "onState": self.onState,
            "onOffState": self.onOffState,
            "brightness": self.brightness,
        }
        if sensorValue is not None:
            self.states["sensorValue"] = sensorValue
        if speedLevel is not None:
            self.states["speedLevel"] = speedLevel
        self.pluginId = ""
        self.deviceTypeId = ""
        self.pluginProps = {}
        self.lastChanged = datetime.datetime.now()

    def replaceOnServer(self):
        pass

    def updateStatesOnServer(self, state_list):
        pass

    def updateStateOnServer(self, key, value):
        self.states[key] = value
        if key == "onOffState":
            self.onOffState = value
            self.onState = value


class Variable:
    def __init__(self, id, name="", value=None):
        self.id = id
        self.name = name or f"Var-{id}"
        self.value = value


indigo_stub = types.SimpleNamespace()
indigo_stub.devices = Devices()
indigo_stub.variables = Variables()
indigo_stub.kProtocol = types.SimpleNamespace(Plugin="Plugin")
indigo_stub.kDeviceAction = types.SimpleNamespace(
    TurnOn=1, TurnOff=2, Toggle=3, RequestStatus=4
)

# Track speed control calls for testing
_speed_control_calls = []


def _set_speed_level(dev_id, value=0):
    _speed_control_calls.append({"dev_id": dev_id, "value": value})
    if dev_id in indigo_stub.devices:
        indigo_stub.devices[dev_id].speedLevel = value


indigo_stub.speedcontrol = types.SimpleNamespace(
    setSpeedLevel=_set_speed_level,
    setSpeedIndex=lambda dev_id, value=0: None,
)

indigo_stub.dimmer = types.SimpleNamespace(
    setBrightness=lambda dev_id, value=0: None,
)


def _create_device(*a, **k):
    new_id = max(indigo_stub.devices.keys(), default=0) + 1
    dev = Device(
        new_id,
        name=k.get("name", ""),
        onState=True,
        brightness=0,
    )
    dev.pluginId = "com.vtmikel.autofan"
    dev.deviceTypeId = k.get("deviceTypeId", "")
    dev.pluginProps = k.get("props", {})
    indigo_stub.devices[new_id] = dev
    return dev


indigo_stub.device = types.SimpleNamespace(
    create=_create_device,
    turnOn=lambda dev_id, **_: setattr(indigo_stub.devices[dev_id], "onState", True),
    turnOff=lambda dev_id, **_: setattr(indigo_stub.devices[dev_id], "onState", False),
    delete=lambda dev_id: indigo_stub.devices.pop(dev_id, None),
)

indigo_stub.variable = types.SimpleNamespace(
    create=lambda *a, **k: indigo_stub.variables.setdefault(
        max(indigo_stub.variables.keys(), default=0) + 1,
        Variable(
            max(indigo_stub.variables.keys(), default=0) + 1,
            name=(a[0] if a else ""),
            value=(a[1] if len(a) > 1 else None),
        ),
    )
)

indigo_stub.server = types.SimpleNamespace(
    log=lambda msg, **kwargs: None,
    getReflectorURL=lambda: None,
)

indigo_stub.Device = Device
indigo_stub.DimmerDevice = Device
indigo_stub.RelayDevice = Device
indigo_stub.SpeedControlDevice = Device
indigo_stub.ThermostatDevice = Device
indigo_stub.SensorDevice = Device
indigo_stub.Variable = Variable


class IndigoDict(dict):
    pass


indigo_stub.Dict = IndigoDict

# Install stub before any imports
sys.modules["indigo"] = indigo_stub

import pytest

# Make plugin code importable
sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            os.pardir,
            "Auto Fan.indigoPlugin",
            "Contents",
            "Server Plugin",
        )
    ),
)


@pytest.fixture(autouse=True)
def fake_indigo():
    """Reset the stub indigo module before each test."""
    indigo_stub.devices.clear()
    indigo_stub.variables.clear()
    _speed_control_calls.clear()
    yield indigo_stub


@pytest.fixture
def speed_control_calls():
    """Access to the log of speed control calls made during the test."""
    return _speed_control_calls

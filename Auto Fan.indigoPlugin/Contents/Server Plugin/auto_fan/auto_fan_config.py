import json
from pathlib import Path
from typing import List

from .auto_fan_base import AutoFanBase
from .fan_zone import FanZone

try:
    import indigo
except ImportError:
    pass


class AutoFanConfig(AutoFanBase):
    """
    Configuration handler for the Auto Fan plugin.
    Loads JSON config, manages zones, and syncs global config device state.
    """

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)

        # Don't sync until schema is loaded and Indigo device exists
        if not hasattr(self, "config_field_schemas"):
            return

        key = name[1:] if name.startswith("_") else name

        if key in self.config_field_schemas:
            try:
                self.sync_indigo_device()
            except Exception:
                self.logger.exception(
                    f"AutoFanConfig: error syncing global config after '{key}' changed"
                )

    def __init__(self, config: str) -> None:
        super().__init__()
        self.log_non_events = False

        # Global config
        self._indigo_dev_id = None
        self._default_lock_duration = 60
        self._default_lock_extension_duration = 30
        self._weather_dev_id = None

        self._zones = []
        self._config_file = config

        # Load schema
        schema_path = (
            Path(__file__).parent.parent
            / "config_web_editor"
            / "config"
            / "config_schema.json"
        )
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)

        # Capture field schemas for state sync
        plugin_props = schema["properties"]["plugin_config"]["properties"]
        self.config_field_schemas = plugin_props
        zone_props = schema["properties"]["zones"]["items"]["properties"]
        self.zone_field_schemas = {}

        def _collect(p):
            for k, v in p.items():
                self.zone_field_schemas[k] = v
                if v.get("type") == "object" and "properties" in v:
                    _collect(v["properties"])

        _collect(zone_props)

        self.load_config()

    @property
    def enabled(self) -> bool:
        """Plugin is enabled/disabled via the global config Indigo device on/off state."""
        return bool(self.indigo_dev.onState)

    @enabled.setter
    def enabled(self, value: bool) -> None:
        dev = self.indigo_dev
        if dev is None:
            return
        if value:
            indigo.device.turnOn(self.indigo_dev.id)
        else:
            indigo.device.turnOff(self.indigo_dev.id)

    @property
    def default_lock_duration(self) -> int:
        return self._default_lock_duration

    @default_lock_duration.setter
    def default_lock_duration(self, value: int) -> None:
        self._default_lock_duration = value

    @property
    def default_lock_extension_duration(self) -> int:
        return self._default_lock_extension_duration

    @default_lock_extension_duration.setter
    def default_lock_extension_duration(self, value: int) -> None:
        self._default_lock_extension_duration = value

    @property
    def weather_dev_id(self) -> int:
        return self._weather_dev_id

    @weather_dev_id.setter
    def weather_dev_id(self, value: int) -> None:
        self._weather_dev_id = value

    def load_config(self) -> None:
        with open(self._config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.from_config_dict(data)

    def from_config_dict(self, data: dict) -> None:
        self._debug_log("from_config_dict called")

        plugin_config = data.get("plugin_config", {})

        for key, value in plugin_config.items():
            if hasattr(self, key):
                setattr(self, key, value)

        # Process zones
        self._zones = []
        zones_data = data.get("zones", [])
        for zone_d in zones_data:
            z = FanZone(zone_d.get("name", "Unnamed"), self)
            z.from_config_dict(zone_d)
            self._zones.append(z)

        # Assign zone indices
        for idx, z in enumerate(self._zones):
            z.zone_index = idx

        # Push initial states to Indigo
        for z in self._zones:
            z.sync_indigo_device()

        # Calculate initial target speeds
        for z in self._zones:
            z.calculate_target_speed()

        self._debug_log("from_config_dict finished")

    @property
    def zones(self) -> List[FanZone]:
        return self._zones

    @property
    def indigo_dev(self):
        """Retrieve or create the Indigo device for global config."""
        if getattr(self, "_indigo_dev_id", None) is not None:
            try:
                return indigo.devices[self._indigo_dev_id]
            except Exception:
                pass

        for d in indigo.devices:
            if (
                d.pluginId == "com.vtmikel.autofan"
                and d.deviceTypeId == "auto_fan_config"
            ):
                self._indigo_dev_id = d.id
                return d

        try:
            dev = indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                name="Auto Fan Global Config",
                address="",
                deviceTypeId="auto_fan_config",
                props={},
            )
            self._indigo_dev_id = dev.id
            indigo.device.turnOn(dev.id)
            self.logger.info(
                f"🆕 Created new Indigo device for Auto Fan Global Config "
                f"(id: {dev.id}, name: {dev.name})"
            )
            return dev
        except Exception as e:
            self.logger.error(f"Error creating global config device: {e}")
            return None

    def _build_schema_states(self, dev):
        """Collect schema-driven config states for Indigo device."""
        states = []
        for key, schema in self.config_field_schemas.items():
            if not schema.get("x-sync_to_indigo"):
                continue
            if key not in dev.states:
                continue
            val = getattr(self, key, None)
            states.append(
                {"key": key, "value": json.dumps(val) if isinstance(val, list) else val}
            )
        return states

    def sync_indigo_device(self) -> None:
        """Sync Indigo device states for global config."""
        dev = self.indigo_dev
        if dev is None:
            self.logger.error("AutoFanConfig: no Indigo device found, skipping sync")
            return
        state_list = self._build_schema_states(dev)
        try:
            dev.updateStatesOnServer(state_list)
        except Exception as e:
            self.logger.error(f"Failed to sync global config device: {e}")

    @property
    def agent(self):
        return getattr(self, "_agent", None)

    @agent.setter
    def agent(self, value):
        self._agent = value

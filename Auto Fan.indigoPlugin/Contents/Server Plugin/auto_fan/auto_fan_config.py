import json
import math
from pathlib import Path
from typing import List

from .auto_fan_base import AutoFanBase
from .fan_zone import FanZone
from .seasons import SEASONS

try:
    import indigo
except ImportError:
    pass


def _convert_dual_curves_to_unified(old_curves: dict) -> dict:
    """Convert legacy dual cooling/warming curves to a unified fan curve."""
    # Merge all breakpoints from both curves into one list
    combined = []
    for curve_key in ("cooling_curve", "warming_curve"):
        curve = old_curves.get(curve_key, {})
        for bp in curve.get("breakpoints", []):
            combined.append({"offset": bp.get("delta", 0), "speed": bp.get("speed_pct", 0)})

    if not combined:
        # Return default curve
        return {
            "temperature_range": 3,
            "num_points": 7,
            "points": [
                {"offset": -3.0, "speed": 0}, {"offset": -2.0, "speed": 10},
                {"offset": -1.0, "speed": 20}, {"offset": 0.0, "speed": 30},
                {"offset": 1.0, "speed": 55}, {"offset": 2.0, "speed": 80},
                {"offset": 3.0, "speed": 100},
            ],
        }

    combined.sort(key=lambda p: p["offset"])

    # Determine range from max absolute offset, clamped to 1-5
    max_abs = max(abs(p["offset"]) for p in combined)
    temp_range = max(1, min(5, int(math.ceil(max_abs))))

    # Resample onto 7-point evenly-spaced grid
    num_points = 7
    points = []
    for i in range(num_points):
        offset = -temp_range + (2 * temp_range * i) / (num_points - 1)
        # Linear interpolation from combined points
        if offset <= combined[0]["offset"]:
            speed = combined[0]["speed"]
        elif offset >= combined[-1]["offset"]:
            speed = combined[-1]["speed"]
        else:
            speed = 0.0
            for j in range(len(combined) - 1):
                if combined[j]["offset"] <= offset <= combined[j + 1]["offset"]:
                    span = combined[j + 1]["offset"] - combined[j]["offset"]
                    if span == 0:
                        speed = combined[j]["speed"]
                    else:
                        t = (offset - combined[j]["offset"]) / span
                        speed = combined[j]["speed"] + t * (combined[j + 1]["speed"] - combined[j]["speed"])
                    break
        points.append({"offset": round(offset, 2), "speed": round(speed)})

    return {"temperature_range": temp_range, "num_points": num_points, "points": points}


def _round_to_nearest(val, step, lo=0, hi=100):
    """Round a numeric value to the nearest multiple of step, clamped to [lo, hi].

    Uses half-away-from-zero rounding (5 → 10, -5 → -10) to preserve user intent.
    """
    if val >= 0:
        rounded = math.floor(val / step + 0.5) * step
    else:
        rounded = math.ceil(val / step - 0.5) * step
    return max(lo, min(hi, rounded))


def _migrate_modifiers(zone_d: dict) -> None:
    """Migrate old-format modifiers (enabled + numeric) to dropdown-based integers.

    Detects old format by presence of 'enabled' key in any modifier sub-dict.
    """
    mods = zone_d.get("modifiers")
    if not mods:
        return

    # HVAC Cooling: enabled + speed_adjust_pct → speed_boost_pct + clamp_min_pct
    cool = mods.get("hvac_cooling_active", {})
    if "enabled" in cool:
        if cool.pop("enabled"):
            old_val = cool.pop("speed_adjust_pct", 15)
            cool["speed_boost_pct"] = _round_to_nearest(max(0, old_val), 10)
        else:
            cool.pop("speed_adjust_pct", None)
            cool["speed_boost_pct"] = 0
        cool.setdefault("clamp_min_pct", 0)

    # HVAC Heating: enabled + speed_adjust_pct (negative) → speed_adjust_pct (bidirectional)
    heat = mods.get("hvac_heating_active", {})
    if "enabled" in heat:
        if heat.pop("enabled"):
            old_val = heat.get("speed_adjust_pct", -20)
            heat["speed_adjust_pct"] = _round_to_nearest(old_val, 10, lo=-100, hi=100)
        else:
            heat["speed_adjust_pct"] = 0
        if "clamp_min_pct" in heat:
            heat["clamp_min_pct"] = _round_to_nearest(heat["clamp_min_pct"], 10)
        else:
            heat["clamp_min_pct"] = 0

    # Nighttime: enabled → implicit via clamp values
    night = mods.get("nighttime", {})
    if "enabled" in night:
        if night.pop("enabled"):
            night["clamp_min_pct"] = _round_to_nearest(night.get("clamp_min_pct", 0), 10)
            night["clamp_max_pct"] = _round_to_nearest(night.get("clamp_max_pct", 100), 10)
        else:
            night["clamp_min_pct"] = 0
            night["clamp_max_pct"] = 100  # Effectively disabled

    # Humidity: enabled + proportional → flat boost
    hum = mods.get("humidity", {})
    if "enabled" in hum:
        if hum.pop("enabled"):
            old_max = hum.pop("max_adjust_pct", 15)
            hum["speed_boost_pct"] = _round_to_nearest(max(0, old_max), 10)
        else:
            hum.pop("max_adjust_pct", None)
            hum["speed_boost_pct"] = 0
        hum.pop("speed_adjust_per_unit_pct", None)
        if "threshold" in hum:
            hum["threshold"] = _round_to_nearest(hum["threshold"], 5, lo=40, hi=80)

    # No Presence: enabled → implicit via clamp value
    no_pres = mods.get("no_presence", {})
    if "enabled" in no_pres:
        if no_pres.pop("enabled"):
            no_pres["clamp_max_pct"] = _round_to_nearest(no_pres.get("clamp_max_pct", 0), 10)
        else:
            no_pres["clamp_max_pct"] = 100  # Effectively disabled

    # Nighttime: flat → per-season
    # After the enabled→flat migration above, detect flat format (has clamp keys
    # directly, not nested under a season) and wrap into all 4 seasons.
    night = mods.get("nighttime", {})
    season_keys = ("spring", "summer", "fall", "winter")
    if night and not any(s in night for s in season_keys):
        flat_values = {
            "clamp_min_pct": night.get("clamp_min_pct", 0),
            "clamp_max_pct": night.get("clamp_max_pct", 100),
            "night_start_hour": night.get("night_start_hour", 22),
            "night_end_hour": night.get("night_end_hour", 8),
        }
        mods["nighttime"] = {s: dict(flat_values) for s in season_keys}


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
            self._migrate_zone(zone_d)
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

    @staticmethod
    def _migrate_zone(zone_d: dict) -> None:
        """Migrate legacy zone config fields to current schema."""
        # humidity_dev_id (single int) → humidity_dev_ids (list)
        if "humidity_dev_id" in zone_d and "humidity_dev_ids" not in zone_d:
            old_val = zone_d.pop("humidity_dev_id")
            zone_d["humidity_dev_ids"] = [old_val] if old_val is not None else []
        elif "humidity_dev_id" in zone_d:
            zone_d.pop("humidity_dev_id")

        # ideal_temp_use_variable (bool) → ideal_temp_source (enum)
        if "ideal_temp_use_variable" in zone_d and "ideal_temp_source" not in zone_d:
            old_val = zone_d.pop("ideal_temp_use_variable")
            zone_d["ideal_temp_source"] = "variable" if old_val else "static"
        elif "ideal_temp_use_variable" in zone_d:
            zone_d.pop("ideal_temp_use_variable")

        # Remove deprecated weather_dev_id_override
        zone_d.pop("weather_dev_id_override", None)

        # speed_curves (dual cooling+warming) → fan_curve (unified)
        if "speed_curves" in zone_d and "fan_curve" not in zone_d:
            old = zone_d.pop("speed_curves")
            zone_d["fan_curve"] = _convert_dual_curves_to_unified(old)
        elif "speed_curves" in zone_d:
            zone_d.pop("speed_curves")

        # fan_curve (single) → seasonal_curves (per-season)
        if "fan_curve" in zone_d and "seasonal_curves" not in zone_d:
            curve = zone_d.pop("fan_curve")
            zone_d["seasonal_curves"] = {s: dict(curve) for s in SEASONS}
        elif "fan_curve" in zone_d and "seasonal_curves" in zone_d:
            zone_d.pop("fan_curve")

        # Migrate old-format modifiers (enabled + numeric) → dropdown integers
        _migrate_modifiers(zone_d)

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

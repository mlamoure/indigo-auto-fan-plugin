import json
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from .auto_fan_base import AutoFanBase
from .hvac_mode import HvacMode, detect_hvac_mode
from .seasons import SEASONS, get_current_season
from .speed_curve import apply_modifiers, calculate_base_speed
from .speed_plan import SpeedPlan
from .utils import get_fan_speed_pct, send_fan_speed

try:
    import indigo
except ImportError:
    pass

# Grace period before unlocking when presence disappears
LOCK_HOLD_GRACE_SECONDS = 30

DEFAULT_FAN_CURVE = {
    "temperature_range": 3,
    "num_points": 7,
    "points": [
        {"offset": -3.0, "speed": 0},
        {"offset": -2.0, "speed": 10},
        {"offset": -1.0, "speed": 20},
        {"offset": 0.0, "speed": 30},
        {"offset": 1.0, "speed": 55},
        {"offset": 2.0, "speed": 80},
        {"offset": 3.0, "speed": 100},
    ],
}


class FanZone(AutoFanBase):
    """
    Represents a fan zone — a single fan device with associated sensors.

    Manages speed calculation, locking, and Indigo device state sync.
    """

    def __init__(self, name: str, config) -> None:
        super().__init__()
        self.name = name
        self._config = config

        # Device IDs
        self.fan_dev_id: Optional[int] = None
        self.temp_sensor_dev_ids: List[int] = []
        self.presence_dev_ids: List[int] = []
        self.thermostat_dev_id: Optional[int] = None
        self.humidity_dev_ids: List[int] = []

        # Temperature settings
        self.ideal_temp_value: float = 72.0
        self.ideal_temp_source: str = "static"
        self.ideal_temp_var_id: Optional[int] = None

        # Seasonal fan curves (one per season)
        self.seasonal_curves: dict = {
            s: dict(DEFAULT_FAN_CURVE, points=[dict(p) for p in DEFAULT_FAN_CURVE["points"]])
            for s in SEASONS
        }

        # Modifiers
        self.modifiers: dict = {}

        # Lock state
        self.locked: bool = False
        self.lock_expiration: Optional[datetime] = None
        self.lock_duration: int = -1  # -1 = use plugin default
        self.lock_extension_duration: int = -1

        # Zone state
        self.enabled: bool = True
        self.zone_index: int = 0
        self._indigo_dev_id: Optional[int] = None
        self._indigo_dev_create_failed: bool = False
        self._target_speed_pct: float = 0.0

        # Runtime state definitions for Indigo device
        self.zone_indigo_device_runtime_states = [
            {"key": "current_temperature", "label": "Current Temperature", "type": "number",
             "getter": lambda: self.get_current_temperature()},
            {"key": "ideal_temperature", "label": "Ideal Temperature", "type": "number",
             "getter": lambda: self.get_ideal_temperature()},
            {"key": "temperature_delta", "label": "Temperature Delta", "type": "number",
             "getter": lambda: self.get_temperature_delta()},
            {"key": "target_speed_pct", "label": "Target Speed %", "type": "number",
             "getter": lambda: self._target_speed_pct},
            {"key": "current_speed_pct", "label": "Current Speed %", "type": "number",
             "getter": lambda: self._get_current_speed_pct()},
            {"key": "hvac_mode", "label": "HVAC Mode", "type": "string",
             "getter": lambda: self.get_hvac_mode().value},
            {"key": "presence_detected", "label": "Presence Detected", "type": "boolean",
             "getter": lambda: self.has_presence_detected()},
            {"key": "zone_locked", "label": "Zone Locked", "type": "boolean",
             "getter": lambda: self.locked},
            {"key": "lock_expiration", "label": "Lock Expiration", "type": "string",
             "getter": lambda: str(self.lock_expiration) if self.lock_expiration else ""},
            {"key": "humidity", "label": "Humidity", "type": "number",
             "getter": lambda: self.get_humidity() or 0},
            {"key": "outdoor_temperature", "label": "Outdoor Temperature", "type": "number",
             "getter": lambda: self.get_outdoor_temperature() or 0},
            {"key": "current_season", "label": "Current Season", "type": "string",
             "getter": lambda: get_current_season()},
        ]

    @property
    def fan_curve(self) -> dict:
        """Return the fan curve for the current season."""
        season = get_current_season()
        return self.seasonal_curves.get(season, self.seasonal_curves.get("summer", {}))

    def from_config_dict(self, data: dict) -> None:
        """Load zone configuration from a parsed JSON dict."""
        self._debug_log("from_config_dict called")

        self.fan_dev_id = data.get("fan_dev_id")
        self.temp_sensor_dev_ids = data.get("temp_sensor_dev_ids", [])
        self.presence_dev_ids = data.get("presence_dev_ids", [])
        self.thermostat_dev_id = data.get("thermostat_dev_id")
        self.humidity_dev_ids = data.get("humidity_dev_ids", [])

        self.ideal_temp_value = data.get("ideal_temp_value", 72.0)
        self.ideal_temp_source = data.get("ideal_temp_source", "static")
        self.ideal_temp_var_id = data.get("ideal_temp_var_id")

        if "seasonal_curves" in data:
            self.seasonal_curves = data["seasonal_curves"]
        elif "fan_curve" in data:
            curve = data["fan_curve"]
            self.seasonal_curves = {s: dict(curve) for s in SEASONS}
        self.modifiers = data.get("modifiers", {})

        self.lock_duration = data.get("lock_duration", -1)
        self.lock_extension_duration = data.get("lock_extension_duration", -1)
        self.enabled = data.get("enabled", True)
        self._indigo_dev_id = data.get("indigo_dev_id")

    # ---- Sensor Reading Methods ----

    def get_current_temperature(self) -> Optional[float]:
        """Get current room temperature (average of multiple sensors if configured)."""
        if not self.temp_sensor_dev_ids:
            return None

        temps = []
        for dev_id in self.temp_sensor_dev_ids:
            try:
                dev = indigo.devices[dev_id]
                for key in ("sensorValue", "temperature", "temp", "Temperature"):
                    if key in dev.states:
                        try:
                            temps.append(float(dev.states[key]))
                            break
                        except (ValueError, TypeError):
                            continue
            except Exception as e:
                self._debug_log(f"Error reading temp sensor {dev_id}: {e}")
                continue

        if not temps:
            return None
        return sum(temps) / len(temps)

    def get_ideal_temperature(self) -> Optional[float]:
        """Get ideal temperature based on configured source."""
        if self.ideal_temp_source == "variable" and self.ideal_temp_var_id:
            try:
                return float(indigo.variables[self.ideal_temp_var_id].value)
            except Exception:
                pass
            return self.ideal_temp_value
        elif self.ideal_temp_source == "thermostat":
            heat = self.get_heat_setpoint()
            cool = self.get_cool_setpoint()
            if heat is not None and cool is not None:
                # Midpoint between heating and cooling thresholds
                return (heat + cool) / 2.0
            elif heat is not None:
                return heat
            elif cool is not None:
                return cool
            return self.ideal_temp_value
        return self.ideal_temp_value

    def get_temperature_delta(self) -> Optional[float]:
        """Get delta: current - ideal. Positive = room warmer than ideal."""
        current = self.get_current_temperature()
        ideal = self.get_ideal_temperature()
        if current is None or ideal is None:
            return None
        return current - ideal

    def get_humidity(self) -> Optional[float]:
        """Get current humidity (average of multiple sensors if configured)."""
        if not self.humidity_dev_ids:
            return None

        readings = []
        for dev_id in self.humidity_dev_ids:
            try:
                dev = indigo.devices[dev_id]
                for key in ("sensorValue", "humidity", "relativeHumidity"):
                    if key in dev.states:
                        try:
                            readings.append(float(dev.states[key]))
                            break
                        except (ValueError, TypeError):
                            continue
            except Exception as e:
                self._debug_log(f"Error reading humidity sensor {dev_id}: {e}")
                continue

        if not readings:
            return None
        return sum(readings) / len(readings)

    def get_outdoor_temperature(self) -> Optional[float]:
        """Get outdoor temperature from global weather device."""
        weather_id = getattr(self._config, "weather_dev_id", None)
        if not weather_id:
            return None
        try:
            dev = indigo.devices[weather_id]
            for key in ("feelslike", "temp", "temperature", "sensorValue"):
                if key in dev.states:
                    try:
                        return float(dev.states[key])
                    except (ValueError, TypeError):
                        continue
        except Exception as e:
            self._debug_log(f"Error reading weather device {weather_id}: {e}")
        return None

    def has_presence_detected(self) -> bool:
        """Check if any presence sensor reports presence."""
        # Assume presence when no sensors configured so fans run by default
        # rather than being permanently off.
        if not self.presence_dev_ids:
            return True

        for dev_id in self.presence_dev_ids:
            try:
                dev = indigo.devices[dev_id]
                if dev.onState:
                    return True
            except Exception:
                continue
        return False

    # ---- HVAC Methods ----

    def get_heat_setpoint(self) -> Optional[float]:
        """Get thermostat heat setpoint."""
        if not self.thermostat_dev_id:
            return None
        try:
            dev = indigo.devices[self.thermostat_dev_id]
            return float(dev.heatSetpoint) if hasattr(dev, "heatSetpoint") else None
        except Exception:
            return None

    def get_cool_setpoint(self) -> Optional[float]:
        """Get thermostat cool setpoint."""
        if not self.thermostat_dev_id:
            return None
        try:
            dev = indigo.devices[self.thermostat_dev_id]
            return float(dev.coolSetpoint) if hasattr(dev, "coolSetpoint") else None
        except Exception:
            return None

    def get_hvac_state(self) -> str:
        """Get current HVAC state (heating, cooling, idle, etc.)."""
        if not self.thermostat_dev_id:
            return "unknown"
        try:
            dev = indigo.devices[self.thermostat_dev_id]
            # Common state keys for HVAC state
            for key in ("hvacOperationMode", "hvac_state", "operating_state"):
                if key in dev.states:
                    return str(dev.states[key]).lower()
            # Check if actively heating or cooling from thermostat properties
            if hasattr(dev, "hvacMode"):
                return str(dev.hvacMode).lower()
        except Exception:
            pass
        return "unknown"

    def is_hvac_cooling(self) -> bool:
        """Check if HVAC is actively cooling."""
        state = self.get_hvac_state()
        return "cool" in state

    def is_hvac_heating(self) -> bool:
        """Check if HVAC is actively heating."""
        state = self.get_hvac_state()
        return "heat" in state

    def get_hvac_mode(self) -> HvacMode:
        """Detect current HVAC mode from thermostat state."""
        return detect_hvac_mode(
            heat_setpoint=self.get_heat_setpoint(),
            cool_setpoint=self.get_cool_setpoint(),
            outdoor_temp=self.get_outdoor_temperature(),
            ideal_temp=self.get_ideal_temperature(),
        )

    # ---- Speed Calculation ----

    def _get_current_speed_pct(self) -> float:
        """Get current fan speed percentage."""
        if not self.fan_dev_id:
            return 0.0
        return get_fan_speed_pct(self.fan_dev_id)

    def calculate_target_speed(self) -> SpeedPlan:
        """
        Calculate the target fan speed based on temperature delta, curves, and modifiers.

        Returns:
            SpeedPlan with target speed and explanations.
        """
        plan = SpeedPlan()

        # Check zone enabled
        if not self.enabled:
            plan.exclusions.append(("⏸️", f"Zone '{self.name}' is disabled"))
            return plan

        # Check global config enabled
        if not self._config.enabled:
            plan.exclusions.append(("⏸️", "Plugin is globally disabled"))
            return plan

        # Get temperature delta
        delta = self.get_temperature_delta()
        if delta is None:
            plan.exclusions.append(
                ("🌡️", "Cannot read temperature — missing sensor data")
            )
            return plan

        ideal = self.get_ideal_temperature()
        current_temp = self.get_current_temperature()
        plan.contributions.append(
            ("🌡️", f"Temp: {current_temp:.1f}°F, Ideal: {ideal:.1f}°F, Delta: {delta:+.1f}°F")
        )

        # Interpolate base speed from fan curve (selected by current season)
        season = get_current_season()
        base_speed = calculate_base_speed(delta, self.fan_curve)
        plan.contributions.append(
            ("📈", f"Fan curve ({season}) → {base_speed:.1f}%")
        )

        # Apply modifiers
        final_speed, modifier_contribs = apply_modifiers(
            base_speed=base_speed,
            modifiers=self.modifiers,
            is_hvac_cooling=self.is_hvac_cooling(),
            is_hvac_heating=self.is_hvac_heating(),
            humidity=self.get_humidity(),
            has_presence=self.has_presence_detected(),
        )
        plan.contributions.extend(modifier_contribs)

        # HVAC mode for logging
        hvac_mode = self.get_hvac_mode()
        plan.contributions.append(("🏠", f"HVAC mode: {hvac_mode.value}"))

        plan.target_speed_pct = final_speed
        self._target_speed_pct = final_speed

        # Determine device changes
        if self.fan_dev_id:
            current_speed = self._get_current_speed_pct()
            speed_int = round(final_speed)
            current_int = round(current_speed)
            if speed_int != current_int:
                plan.device_changes.append(
                    ("🌀", f"Set fan to {speed_int}% (was {current_int}%)")
                )

        return plan

    def has_speed_change(self) -> bool:
        """Check if target speed differs from current fan speed."""
        if not self.fan_dev_id:
            return False
        current = round(self._get_current_speed_pct())
        target = round(self._target_speed_pct)
        return current != target

    def apply_speed_change(self) -> bool:
        """Apply the calculated target speed to the fan device."""
        if not self.fan_dev_id:
            return False
        return send_fan_speed(self.fan_dev_id, self._target_speed_pct, self.logger)

    # ---- Lock Management ----

    def get_effective_lock_duration(self) -> int:
        """Get lock duration for this zone (zone override or plugin default)."""
        if self.lock_duration >= 0:
            return self.lock_duration
        return self._config.default_lock_duration

    def get_effective_lock_extension_duration(self) -> int:
        """Get lock extension duration for this zone."""
        if self.lock_extension_duration >= 0:
            return self.lock_extension_duration
        return self._config.default_lock_extension_duration

    def lock_zone(self, reason: str = "manual change") -> None:
        """Lock the zone to prevent automation."""
        duration = self.get_effective_lock_duration()
        self.locked = True
        self.lock_expiration = datetime.now() + timedelta(minutes=duration)
        self.logger.info(
            f"🔒 Zone '{self.name}' locked for {duration}m ({reason}). "
            f"Expires: {self.lock_expiration.strftime('%H:%M:%S')}"
        )

    def extend_lock(self) -> None:
        """Extend the lock if presence is still detected."""
        if not self.locked:
            return
        extension = self.get_effective_lock_extension_duration()
        self.lock_expiration = datetime.now() + timedelta(minutes=extension)
        self._debug_log(
            f"Lock extended by {extension}m. New expiration: {self.lock_expiration.strftime('%H:%M:%S')}"
        )

    def unlock_zone(self) -> None:
        """Unlock the zone."""
        self.locked = False
        self.lock_expiration = None
        self.logger.info(f"🔓 Zone '{self.name}' unlocked")

    def is_lock_expired(self) -> bool:
        """Check if the lock has expired."""
        if not self.locked or self.lock_expiration is None:
            return False
        return datetime.now() >= self.lock_expiration

    # ---- Device Change Detection ----

    def has_device(self, dev_id: int) -> bool:
        """Check if a device ID belongs to this zone."""
        if dev_id == self.fan_dev_id:
            return True
        if dev_id in self.temp_sensor_dev_ids:
            return True
        if dev_id in self.presence_dev_ids:
            return True
        if dev_id == self.thermostat_dev_id:
            return True
        if dev_id in self.humidity_dev_ids:
            return True
        weather_id = getattr(self._config, "weather_dev_id", None)
        if dev_id == weather_id:
            return True
        return False

    def is_fan_device(self, dev_id: int) -> bool:
        """Check if a device ID is this zone's fan device."""
        return dev_id == self.fan_dev_id

    def has_variable(self, var_id: int) -> bool:
        """Check if a variable ID is relevant to this zone."""
        if self.ideal_temp_source == "variable" and var_id == self.ideal_temp_var_id:
            return True
        return False

    # ---- Indigo Device Sync ----

    @property
    def indigo_dev(self):
        """Retrieve or create the Indigo device for this zone."""
        if self._indigo_dev_create_failed:
            return None
        if self._indigo_dev_id is not None:
            try:
                return indigo.devices[self._indigo_dev_id]
            except Exception:
                pass

        # Search for existing device
        for d in indigo.devices:
            if (
                d.pluginId == "com.vtmikel.autofan"
                and d.deviceTypeId == "auto_fan_zone"
                and int(d.pluginProps.get("zone_index", -1)) == self.zone_index
            ):
                self._indigo_dev_id = d.id
                return d

        # Create new device
        expected_name = f"Auto Fan - {self.name}"
        try:
            dev = indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                name=expected_name,
                address="",
                deviceTypeId="auto_fan_zone",
                props={"zone_index": str(self.zone_index)},
            )
            self._indigo_dev_id = dev.id
            indigo.device.turnOn(dev.id)
            self.logger.info(
                f"🆕 Created Indigo device for zone '{self.name}' (id: {dev.id})"
            )
            return dev
        except Exception as e:
            # If name already exists, adopt the existing device
            for d in indigo.devices:
                if (
                    d.pluginId == "com.vtmikel.autofan"
                    and d.deviceTypeId == "auto_fan_zone"
                    and d.name == expected_name
                ):
                    self._indigo_dev_id = d.id
                    props = d.pluginProps
                    props["zone_index"] = str(self.zone_index)
                    d.replacePluginPropsOnServer(props)
                    self.logger.info(
                        f"🔗 Adopted existing device '{d.name}' (id: {d.id}) for zone '{self.name}'"
                    )
                    return d
            self._indigo_dev_create_failed = True
            self.logger.error(f"Error creating zone device for '{self.name}': {e}")
            return None

    def sync_indigo_device(self) -> None:
        """Push runtime and schema states to Indigo device."""
        dev = self.indigo_dev
        if dev is None:
            return

        state_list = []

        # Schema-driven states
        for key, schema in self._config.zone_field_schemas.items():
            if not schema.get("x-sync_to_indigo"):
                continue
            if key not in dev.states:
                continue
            val = getattr(self, key, None)
            if val is None:
                continue
            state_list.append(
                {"key": key, "value": json.dumps(val) if isinstance(val, list) else val}
            )

        # Runtime states
        for entry in self.zone_indigo_device_runtime_states:
            key = entry["key"]
            if key not in dev.states:
                continue
            try:
                val = entry["getter"]()
                state_list.append({"key": key, "value": val})
            except Exception:
                continue

        try:
            dev.updateStatesOnServer(state_list)
        except Exception as e:
            self.logger.error(f"Failed to sync zone device '{self.name}': {e}")

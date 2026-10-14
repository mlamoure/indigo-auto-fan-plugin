import json
from datetime import datetime, timedelta

from . import clock
from typing import List, Optional, Tuple

from .auto_fan_base import AutoFanBase
from .seasons import SEASONS, get_current_season
from .comfort import temporary_offset_for_change
from .speed_curve import apply_modifiers, calculate_base_speed
from .speed_plan import SpeedPlan
from .utils import get_fan_speed_pct, send_fan_speed, is_baf_fan, BAF_SPEED_COUNT

try:
    import indigo
except ImportError:
    pass

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

        # Temperature settings (per-season)
        self.seasonal_ideal_temp: dict = {
            s: {
                "source": "static",
                "value": 72.0,
                "var_id": None,
                "max_temp_enabled": False,
                "max_temp_value": None,
            }
            for s in SEASONS
        }

        # Seasonal fan curves (one per season)
        self.seasonal_curves: dict = {
            s: dict(DEFAULT_FAN_CURVE, points=[dict(p) for p in DEFAULT_FAN_CURVE["points"]])
            for s in SEASONS
        }

        # Modifiers
        self.modifiers: dict = {}

        # Night window (per season): when this zone counts as "night"
        self.night_window: dict = {
            s: {"start_hour": 22, "end_hour": 8} for s in SEASONS
        }

        # Lock state
        self.locked: bool = False
        self.lock_expiration: Optional[datetime] = None
        self.lock_duration: int = -1  # -1 = use plugin default
        self.lock_extension_duration: int = -1
        self.hold_locks_at_night: str = "inherit"  # inherit | on | off
        self.lock_rule: Optional[str] = None       # "night_end" | "duration"
        self.lock_reason: str = ""
        self.lock_started: Optional[datetime] = None
        self.lock_step: Optional[int] = None       # fan step when the lock was taken
        self.lock_extensions: int = 0

        # Comfort offsets (degrees added to the configured ideal)
        self.comfort_offset: dict = {s: {"day": 0.0, "night": 0.0} for s in SEASONS}
        self.learning_mode: str = "inherit"  # inherit | suggest | off
        self.temporary_offset: Optional[float] = None
        self.temporary_offset_expires: Optional[datetime] = None
        self.temporary_offset_source: str = ""
        self.temporary_offset_implied_ideal: Optional[float] = None
        self._last_speed_command_time: Optional[datetime] = None
        # The speed step (index, or rounded % for step-less fans) the plugin
        # last asked for; a fan report that lands on it is the plugin's own.
        self._last_commanded_step: Optional[int] = None

        # Zone state
        # `enabled` is exposed as a property below — backed by the Indigo zone
        # device's onOffState so toggles in the Indigo UI take effect. The
        # private `_enabled` is the fallback used until the device is known.
        self._enabled: bool = True
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
            {"key": "configured_ideal_temperature", "label": "Configured Ideal Temperature", "type": "number",
             "getter": lambda: self.get_configured_ideal_temperature()},
            {"key": "comfort_offset_standing", "label": "Standing Comfort Offset", "type": "number",
             "getter": lambda: self.standing_offset_value()},
            {"key": "comfort_offset_temporary", "label": "Temporary Comfort Offset", "type": "number",
             "getter": lambda: (self.temporary_offset if self.temporary_offset_active() else 0.0)},
            {"key": "open_recommendations", "label": "Open Recommendations", "type": "number",
             "getter": lambda: self.open_recommendation_count()},
            {"key": "comfort_offset_expires", "label": "Temporary Offset Expires", "type": "string",
             "getter": lambda: (self.temporary_offset_expires.strftime("%Y-%m-%d %H:%M")
                                if self.temporary_offset_active() and self.temporary_offset_expires else "")},
            {"key": "temperature_delta", "label": "Temperature Delta", "type": "number",
             "getter": lambda: self.get_temperature_delta()},
            {"key": "target_speed_pct", "label": "Target Speed %", "type": "number",
             "getter": lambda: self._target_speed_pct},
            {"key": "current_speed_pct", "label": "Current Speed %", "type": "number",
             "getter": lambda: self._get_current_speed_pct()},
            {"key": "presence_detected", "label": "Presence Detected", "type": "boolean",
             "getter": lambda: self.has_presence_detected()},
            {"key": "zone_locked", "label": "Zone Locked", "type": "boolean",
             "getter": lambda: self.locked},
            {"key": "lock_expiration", "label": "Lock Expiration", "type": "string",
             "getter": lambda: str(self.lock_expiration) if self.lock_expiration else ""},
            {"key": "lock_rule", "label": "Lock Rule", "type": "string",
             "getter": lambda: self.lock_rule or ""},
            {"key": "lock_reason", "label": "Lock Reason", "type": "string",
             "getter": lambda: self.lock_reason if self.locked else ""},
            {"key": "humidity", "label": "Humidity", "type": "number",
             "getter": lambda: self.get_humidity() or 0},
            {"key": "outdoor_temperature", "label": "Outdoor Temperature", "type": "number",
             "getter": lambda: self.get_outdoor_temperature() or 0},
            {"key": "current_season", "label": "Current Season", "type": "string",
             "getter": lambda: get_current_season(**self._config.get_season_kwargs())},
            {"key": "is_night", "label": "Night Window Active", "type": "boolean",
             "getter": lambda: self.is_night()},
        ]

    @property
    def enabled(self) -> bool:
        """Whether this zone's automation is active.

        Backed by the Indigo zone device's onState. Calls `self.indigo_dev`
        to trigger zone_index-based adoption when `_indigo_dev_id` isn't
        yet populated — needed because `auto_fan_conf.json` stores
        `indigo_dev_id: null` and the link is rebuilt lazily on each
        config reload.
        """
        dev = self.indigo_dev
        if dev is not None:
            try:
                return bool(dev.onState)
            except Exception:
                pass
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)
        dev = self.indigo_dev
        if dev is not None:
            try:
                if value:
                    indigo.device.turnOn(dev.id)
                else:
                    indigo.device.turnOff(dev.id)
            except Exception:
                pass

    @property
    def _current_ideal_temp_config(self) -> dict:
        """Return the ideal temp config for the current season."""
        season = get_current_season(**self._config.get_season_kwargs())
        return self.seasonal_ideal_temp.get(
            season, self.seasonal_ideal_temp.get("summer", {})
        )

    @property
    def fan_curve(self) -> dict:
        """Return the fan curve for the current season."""
        season = get_current_season(**self._config.get_season_kwargs())
        return self.seasonal_curves.get(season, self.seasonal_curves.get("summer", {}))

    def from_config_dict(self, data: dict) -> None:
        """Load zone configuration from a parsed JSON dict."""
        self._debug_log("from_config_dict called")

        self.fan_dev_id = data.get("fan_dev_id")
        self.temp_sensor_dev_ids = data.get("temp_sensor_dev_ids", [])
        self.presence_dev_ids = data.get("presence_dev_ids", [])
        self.thermostat_dev_id = data.get("thermostat_dev_id")
        self.humidity_dev_ids = data.get("humidity_dev_ids", [])

        if "seasonal_ideal_temp" in data:
            self.seasonal_ideal_temp = data["seasonal_ideal_temp"]

        if "seasonal_curves" in data:
            self.seasonal_curves = data["seasonal_curves"]
        elif "fan_curve" in data:
            curve = data["fan_curve"]
            self.seasonal_curves = {
                s: {**curve, "points": [dict(p) for p in curve.get("points", [])]}
                for s in SEASONS
            }
        self.modifiers = data.get("modifiers", {})

        if isinstance(data.get("night_window"), dict):
            for season in SEASONS:
                w = data["night_window"].get(season)
                if isinstance(w, dict):
                    self.night_window[season] = {
                        "start_hour": int(w.get("start_hour", 22)),
                        "end_hour": int(w.get("end_hour", 8)),
                    }

        self.lock_duration = data.get("lock_duration", -1)
        self.lock_extension_duration = data.get("lock_extension_duration", -1)
        self.hold_locks_at_night = str(data.get("hold_locks_at_night", "inherit") or "inherit")
        self.learning_mode = str(data.get("learning_mode", "inherit") or "inherit")
        if isinstance(data.get("comfort_offset"), dict):
            for season in SEASONS:
                values = data["comfort_offset"].get(season)
                if isinstance(values, dict):
                    for period in ("day", "night"):
                        try:
                            self.comfort_offset[season][period] = float(values.get(period, 0.0) or 0.0)
                        except (TypeError, ValueError):
                            self.comfort_offset[season][period] = 0.0
        # Assign the backing field directly rather than via the property
        # setter — on reload the Indigo device's onState is the live truth
        # (it reflects any UI toggles made since the last config save), so
        # we don't want to clobber it with a possibly-stale JSON value.
        self._enabled = data.get("enabled", True)
        self._indigo_dev_id = data.get("indigo_dev_id")

    # ---- Night Window ----

    def get_night_window(self, season: Optional[str] = None) -> Tuple[int, int]:
        """Return (start_hour, end_hour) of the night window for a season."""
        season = season or get_current_season(**self._config.get_season_kwargs())
        w = self.night_window.get(season) or {}
        return int(w.get("start_hour", 22)), int(w.get("end_hour", 8))

    def is_night(self, season: Optional[str] = None, now: Optional[datetime] = None) -> bool:
        """True when ``now`` falls inside the zone's night window for the season.

        A window that crosses midnight (22 → 8) is night from 22:00 until 07:59;
        one that does not (0 → 6) is night from 00:00 until 05:59. Equal hours
        mean "never night".
        """
        start_hour, end_hour = self.get_night_window(season)
        if start_hour == end_hour:
            return False
        hour = (now or clock.now()).hour
        if start_hour > end_hour:
            return hour >= start_hour or hour < end_hour
        return start_hour <= hour < end_hour

    def next_night_end(self, now: Optional[datetime] = None) -> datetime:
        """The next moment the current season's night window ends (end_hour:00)."""
        now = now or clock.now()
        _, end_hour = self.get_night_window()
        candidate = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    def next_night_start(self, now: Optional[datetime] = None) -> datetime:
        """The next moment the current season's night window starts (start_hour:00)."""
        now = now or clock.now()
        start_hour, _ = self.get_night_window()
        candidate = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

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

    def get_sensor_readings(self) -> List[Tuple[int, str, Optional[float]]]:
        """Get individual temperature sensor readings.

        Returns:
            List of (dev_id, dev_name, reading_or_None) for each configured sensor.
        """
        readings = []
        for dev_id in self.temp_sensor_dev_ids:
            try:
                dev = indigo.devices[dev_id]
                name = dev.name
                value = None
                for key in ("sensorValue", "temperature", "temp", "Temperature"):
                    if key in dev.states:
                        try:
                            value = float(dev.states[key])
                            break
                        except (ValueError, TypeError):
                            continue
                readings.append((dev_id, name, value))
            except Exception:
                readings.append((dev_id, f"Unknown (id:{dev_id})", None))
        return readings

    def _resolve_thermostat_ideal(self, fallback: float) -> float:
        """Resolve the uncapped thermostat-source ideal temperature."""
        heat = self.get_heat_setpoint()
        cool = self.get_cool_setpoint()
        mode = self.get_hvac_mode()
        # Respect the thermostat's operation mode: a thermostat in
        # cool-only or heat-only mode may still report a stale setpoint
        # for the inactive mode, which would skew the midpoint.
        if "cool" in mode and "heat" not in mode and cool is not None:
            return cool
        if "heat" in mode and "cool" not in mode and heat is not None:
            return heat
        if heat is not None and cool is not None:
            return (heat + cool) / 2.0
        if heat is not None:
            return heat
        if cool is not None:
            return cool
        return fallback

    @staticmethod
    def _get_max_temp_cap(cfg: dict) -> Optional[float]:
        """Return the enabled maximum ideal temp cap, or None if disabled/invalid."""
        if not cfg.get("max_temp_enabled"):
            return None
        try:
            return float(cfg["max_temp_value"])
        except (KeyError, TypeError, ValueError):
            return None

    def get_configured_ideal_temperature(self) -> Optional[float]:
        """Ideal temperature from the configured source for the current season
        (static value, Indigo variable, or thermostat setpoints with cap),
        before any comfort offset."""
        cfg = self._current_ideal_temp_config
        source = cfg.get("source", "static")
        fallback = cfg.get("value", 72.0)
        var_id = cfg.get("var_id")

        if source == "variable" and var_id:
            try:
                return float(indigo.variables[var_id].value)
            except Exception:
                pass
            return fallback
        elif source == "thermostat":
            resolved = self._resolve_thermostat_ideal(fallback)
            cap = self._get_max_temp_cap(cfg)
            if cap is not None and resolved > cap:
                return cap
            return resolved
        return fallback

    def standing_offset_value(self, season: Optional[str] = None, is_night: Optional[bool] = None) -> float:
        """Standing comfort offset for the season and half of the day."""
        season = season or get_current_season(**self._config.get_season_kwargs())
        if is_night is None:
            is_night = self.is_night(season)
        values = self.comfort_offset.get(season) or {}
        try:
            return float(values.get("night" if is_night else "day", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def open_recommendation_count(self) -> int:
        agent = getattr(self._config, "agent", None)
        recommender = getattr(agent, "recommender", None)
        if recommender is None:
            return 0
        try:
            return len(recommender.open_for_zone(self.name))
        except Exception:
            return 0

    def get_ideal_temperature(self) -> Optional[float]:
        """Effective ideal: configured ideal + standing offset + temporary offset."""
        configured = self.get_configured_ideal_temperature()
        if configured is None:
            return None
        ideal = configured + self.standing_offset_value()
        if self.temporary_offset_active():
            ideal += self.temporary_offset
        return ideal

    # ---- Comfort offsets ----

    def temporary_offset_active(self, now: Optional[datetime] = None) -> bool:
        """True while a temporary offset is set and not yet expired. An expired
        offset is cleared on the spot."""
        if self.temporary_offset is None:
            return False
        if self.temporary_offset_expires is not None and (now or clock.now()) >= self.temporary_offset_expires:
            self.clear_temporary_offset("expired")
            return False
        return True

    def set_temporary_offset(self, offset: float, expires: Optional[datetime], source: str,
                             implied_ideal: Optional[float] = None) -> None:
        self.temporary_offset = float(offset)
        self.temporary_offset_expires = expires
        self.temporary_offset_source = source
        self.temporary_offset_implied_ideal = implied_ideal
        until = expires.strftime("%a %H:%M") if expires else "cleared by hand"
        detail = f", implied ideal {implied_ideal:.1f}°F" if implied_ideal is not None else ""
        self.logger.info(
            f"🎚️ Zone '{self.name}': temporary comfort offset {offset:+.1f}°F until {until} ({source}{detail})"
        )

    def clear_temporary_offset(self, reason: str = "cleared") -> None:
        if self.temporary_offset is None:
            return
        self.logger.info(f"🎚️ Zone '{self.name}': temporary comfort offset {self.temporary_offset:+.1f}°F ended ({reason})")
        self.temporary_offset = None
        self.temporary_offset_expires = None
        self.temporary_offset_source = ""
        self.temporary_offset_implied_ideal = None

    def temporary_offset_expiry_for(self, now: Optional[datetime] = None) -> datetime:
        """A change made at night lasts until the night window ends; a change
        made by day lasts until it begins."""
        now = now or clock.now()
        return self.next_night_end(now) if self.is_night(now=now) else self.next_night_start(now)

    def active_additive_boost(self, season: Optional[str] = None) -> float:
        """Sum of the additive modifiers active right now (HVAC boost/adjust,
        humidity boost), so a chosen speed can be mapped back to the curve."""
        boost = 0.0
        mods = self.modifiers or {}
        if self.is_hvac_cooling():
            boost += float(mods.get("hvac_cooling_active", {}).get("speed_boost_pct", 0) or 0)
        if self.is_hvac_heating():
            boost += float(mods.get("hvac_heating_active", {}).get("speed_adjust_pct", 0) or 0)
        humidity = self.get_humidity()
        hum = mods.get("humidity", {})
        if humidity is not None and hum.get("speed_boost_pct", 0) and humidity > hum.get("threshold", 60):
            boost += float(hum.get("speed_boost_pct", 0))
        return boost

    def derive_temporary_offset(self, human_pct: float) -> Optional[Tuple[float, float, datetime]]:
        """From the speed a person chose, work out (implied_ideal, offset, expires).
        The offset is relative to the configured ideal plus the standing offset,
        so it captures only what the person asked for beyond the standing value."""
        season = get_current_season(**self._config.get_season_kwargs())
        base_ideal = self.get_configured_ideal_temperature()
        if base_ideal is None:
            return None
        base_ideal += self.standing_offset_value(season)
        result = temporary_offset_for_change(
            human_pct, self.fan_curve.get("points", []), self.get_current_temperature(),
            base_ideal, self.active_additive_boost(season),
        )
        if result is None:
            return None
        implied_ideal, offset = result
        return implied_ideal, offset, self.temporary_offset_expiry_for()

    def offset_state_dict(self) -> Optional[dict]:
        if not self.temporary_offset_active():
            return None
        return {
            "offset": self.temporary_offset,
            "expires": self.temporary_offset_expires.isoformat(timespec="seconds") if self.temporary_offset_expires else None,
            "source": self.temporary_offset_source,
            "implied_ideal": self.temporary_offset_implied_ideal,
        }

    def restore_offset_state(self, state: dict) -> bool:
        try:
            offset = float(state["offset"])
            expires = datetime.fromisoformat(state["expires"]) if state.get("expires") else None
        except (KeyError, TypeError, ValueError):
            return False
        if expires is not None and expires <= clock.now():
            return False
        self.temporary_offset = offset
        self.temporary_offset_expires = expires
        self.temporary_offset_source = state.get("source") or "restored"
        self.temporary_offset_implied_ideal = state.get("implied_ideal")
        return True

    def get_ideal_temperature_breakdown(self) -> List[Tuple[str, str]]:
        """Explain how the ideal temperature was resolved.

        Returns:
            List of (emoji, message) tuples describing the resolution.
        """
        cfg = self._current_ideal_temp_config
        source = cfg.get("source", "static")
        fallback = cfg.get("value", 72.0)
        var_id = cfg.get("var_id")
        season = get_current_season(**self._config.get_season_kwargs())

        lines: List[Tuple[str, str]] = []
        lines.append(("📅", f"Season: {season}"))

        if source == "variable" and var_id:
            try:
                var = indigo.variables[var_id]
                var_value = float(var.value)
                lines.append(("🔢", f"Source: variable '{var.name}' (id:{var.id}) = {var_value:.1f}°F"))
            except Exception:
                lines.append(("⚠️", f"Source: variable (id:{var_id}) — read failed, using fallback {fallback:.1f}°F"))
        elif source == "thermostat":
            heat = self.get_heat_setpoint()
            cool = self.get_cool_setpoint()
            mode = self.get_hvac_mode()
            tstat_name = "Unknown"
            if self.thermostat_dev_id:
                try:
                    tstat_name = indigo.devices[self.thermostat_dev_id].name
                except Exception:
                    pass
            lines.append(("🌡️", f"Source: thermostat '{tstat_name}' (id:{self.thermostat_dev_id})"))
            if "cool" in mode and "heat" not in mode and cool is not None:
                lines.append(("", f"  HVAC mode: cool → using cool setpoint: {cool:.1f}°F"))
            elif "heat" in mode and "cool" not in mode and heat is not None:
                lines.append(("", f"  HVAC mode: heat → using heat setpoint: {heat:.1f}°F"))
            elif heat is not None and cool is not None:
                lines.append(("", f"  Heat setpoint: {heat:.1f}°F, Cool setpoint: {cool:.1f}°F"))
                lines.append(("", f"  Midpoint: ({heat:.1f} + {cool:.1f}) / 2 = {(heat + cool) / 2.0:.1f}°F"))
            elif heat is not None:
                lines.append(("", f"  Heat setpoint only: {heat:.1f}°F"))
            elif cool is not None:
                lines.append(("", f"  Cool setpoint only: {cool:.1f}°F"))
            else:
                lines.append(("⚠️", f"  No setpoints available, using fallback {fallback:.1f}°F"))
            if cfg.get("max_temp_enabled"):
                cap = self._get_max_temp_cap(cfg)
                uncapped = self._resolve_thermostat_ideal(fallback)
                if cap is None:
                    lines.append(("⚠️", "  Maximum ideal cap enabled but no valid value — ignored"))
                elif uncapped > cap:
                    lines.append(("🧢", f"  Maximum ideal cap: {cap:.1f}° — capped from {uncapped:.1f}°"))
                else:
                    lines.append(("", f"  Maximum ideal cap: {cap:.1f}° (not reached)"))
        else:
            lines.append(("📌", f"Source: static = {fallback:.1f}°F"))

        configured = self.get_configured_ideal_temperature()
        standing = self.standing_offset_value()
        if standing:
            lines.append(("🎚️", f"Standing comfort offset: {standing:+.1f}°F"))
        if self.temporary_offset_active():
            until = self.temporary_offset_expires.strftime("%a %H:%M") if self.temporary_offset_expires else "cleared by hand"
            lines.append(("🎚️", f"Temporary comfort offset: {self.temporary_offset:+.1f}°F until {until} ({self.temporary_offset_source})"))
        effective = self.get_ideal_temperature()
        if configured is not None and effective is not None and abs(effective - configured) > 1e-9:
            lines.append(("🎯", f"Effective ideal: {effective:.1f}°F"))

        return lines

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

    def get_hvac_mode(self) -> str:
        """Get thermostat HVAC operation mode (e.g. heat, cool, heatcool, off), lowercased."""
        if not self.thermostat_dev_id:
            return "unknown"
        try:
            dev = indigo.devices[self.thermostat_dev_id]
            if hasattr(dev, "hvacMode"):
                return str(dev.hvacMode).lower()
        except Exception:
            pass
        return "unknown"

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

    # ---- Speed Calculation ----

    def _get_current_speed_pct(self) -> float:
        """Get current fan speed percentage."""
        if not self.fan_dev_id:
            return 0.0
        return get_fan_speed_pct(self.fan_dev_id)

    def _get_device_speed_info(self) -> dict:
        """Get speed-related device info for SpeedControl devices.

        Returns dict with speed_index, speed_index_count, speed_label
        if the device is a SpeedControl. Empty dict otherwise.
        """
        if not self.fan_dev_id:
            return {}
        try:
            dev = indigo.devices[self.fan_dev_id]
            if is_baf_fan(dev):
                try:
                    baf_speed = int(dev.states.get("baf_speed", 0))
                except (ValueError, TypeError):
                    baf_speed = 0
                return {
                    "speed_index": baf_speed,
                    "speed_index_count": BAF_SPEED_COUNT,
                }
            if hasattr(dev, "speedIndex") and hasattr(dev, "speedIndexCount"):
                info = {
                    "speed_index": int(dev.speedIndex),
                    "speed_index_count": int(dev.speedIndexCount),
                }
                if "speedIndex.ui" in dev.states:
                    info["speed_label"] = str(dev.states["speedIndex.ui"])
                return info
        except Exception as e:
            self._debug_log(f"Error reading speed info for device {self.fan_dev_id}: {e}")
        return {}

    @staticmethod
    def _pct_to_speed_index(pct: float, index_count: int) -> int:
        """Map a percentage (0-100) to a discrete speed index."""
        if index_count <= 1:
            return 0
        idx = round(pct * (index_count - 1) / 100.0)
        return max(0, min(idx, index_count - 1))

    @staticmethod
    def _speed_index_to_pct(index: int, index_count: int) -> float:
        """Map a speed index back to its canonical percentage."""
        if index_count <= 1:
            return 0.0
        return round(index * 100.0 / (index_count - 1))

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
        season = get_current_season(**self._config.get_season_kwargs())
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
            is_home=self._config.is_home(),
            season=season,
            is_night=self.is_night(season),
        )
        plan.contributions.extend(modifier_contribs)

        plan.target_speed_pct = final_speed
        self._target_speed_pct = final_speed

        # Determine device changes
        if self.fan_dev_id and self.has_speed_change():
            from_str, to_str = self.get_speed_change_description()
            plan.device_changes.append(
                ("🌀", f"Fan speed: {from_str} → {to_str}")
            )

        return plan

    def has_speed_change(self) -> bool:
        """Check if target speed differs from current fan speed.

        For SpeedControl devices, compares at the speed index level to avoid
        phantom changes when different percentages map to the same discrete
        speed (e.g., 20% and 33% both map to index 1 on a 4-speed fan).
        For Dimmer/Relay devices, compares rounded percentages.
        """
        if not self.fan_dev_id:
            return False
        info = self._get_device_speed_info()
        if info.get("speed_index_count"):
            current_idx = info["speed_index"]
            target_idx = self._pct_to_speed_index(
                self._target_speed_pct, info["speed_index_count"]
            )
            return current_idx != target_idx
        current = round(self._get_current_speed_pct())
        target = round(self._target_speed_pct)
        return current != target

    def get_speed_change_description(self) -> tuple:
        """Return (from_str, to_str) for logging the speed change.

        For SpeedControl: uses index-mapped percentages (honest about what
        the fan actually does). For Dimmer/Relay: uses rounded percentages.
        """
        info = self._get_device_speed_info()
        if info.get("speed_index_count"):
            count = info["speed_index_count"]
            current_idx = info["speed_index"]
            target_idx = self._pct_to_speed_index(self._target_speed_pct, count)
            current_pct = self._speed_index_to_pct(current_idx, count)
            target_pct = self._speed_index_to_pct(target_idx, count)
            return (f"{current_pct}%", f"{target_pct}%")
        return (
            f"{round(self._get_current_speed_pct())}%",
            f"{round(self._target_speed_pct)}%",
        )

    def apply_speed_change(self) -> bool:
        """Apply the calculated target speed to the fan device."""
        if not self.fan_dev_id:
            return False
        self._last_speed_command_time = clock.now()
        self._last_commanded_step = self._step_for_pct(self._target_speed_pct)
        return send_fan_speed(self.fan_dev_id, self._target_speed_pct, self.logger)

    def _step_for_pct(self, pct: float) -> int:
        """Map a percentage to the fan's step: speed index for stepped fans,
        the rounded percentage for dimmers and relays."""
        info = self._get_device_speed_info()
        if info.get("speed_index_count"):
            return self._pct_to_speed_index(pct, info["speed_index_count"])
        return int(round(pct))

    def acknowledge_current_step(self) -> None:
        """The plugin evaluated and agrees with the step the fan is on: treat
        that step as "ours" so a later report of it is not read as a person."""
        if self.fan_dev_id:
            self._last_commanded_step = self.current_step()

    def current_step(self) -> int:
        """The step the fan is on right now (same scale as _step_for_pct)."""
        info = self._get_device_speed_info()
        if info.get("speed_index_count"):
            return int(info["speed_index"])
        return int(round(self._get_current_speed_pct()))

    def is_change_from_plugin(self, grace_seconds: int = 10) -> bool:
        """Decide whether a fan state change is the plugin's own doing.

        Two tests, either of which means "ours":
        1. The fan has settled on the step the plugin last commanded. The
           Haiku reports its real state 20–50 s after a command, so a timing
           test alone mistook that echo for a person.
        2. The report arrived within ``grace_seconds`` of the command, when a
           fan may still be mid-update (one state key changed, the next not yet).

        Anything else is a person.
        """
        if self._last_commanded_step is not None and self.current_step() == self._last_commanded_step:
            return True
        return self.is_self_triggered_change(grace_seconds)

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

    def holds_locks_at_night(self) -> bool:
        """Zone override ("on"/"off") or the plugin-wide default ("inherit")."""
        if self.hold_locks_at_night == "on":
            return True
        if self.hold_locks_at_night == "off":
            return False
        return bool(getattr(self._config, "hold_locks_at_night", True))

    def get_max_lock_hours(self) -> float:
        return float(getattr(self._config, "max_lock_hours", 10) or 10)

    def lock_zone(self, reason: str = "manual change") -> None:
        """Lock the zone so automation leaves the fan alone.

        Two rules, chosen when the lock is taken:
        - night_end: a manual change inside the zone's Night Window, with
          "Hold locks at nighttime" on, holds until the window ends (or the
          house goes Away).
        - duration: otherwise the configured duration; at expiry the lock is
          extended while presence is detected, up to the maximum lock hours.

        A lock taken while already locked (a re-adjustment) restarts the
        lock with the same decision logic.
        """
        now = clock.now()
        restarted = self.locked
        self.locked = True
        self.lock_started = now
        self.lock_reason = reason
        self.lock_extensions = 0
        self.lock_step = self.current_step() if self.fan_dev_id else None
        if self.holds_locks_at_night() and self.is_night(now=now):
            self.lock_rule = "night_end"
            self.lock_expiration = self.next_night_end(now)
            self.logger.info(
                f"🔒 Zone '{self.name}' locked until night ends at "
                f"{self.lock_expiration.strftime('%H:%M')} ({reason})"
                + (" — lock restarted" if restarted else "")
            )
        else:
            self.lock_rule = "duration"
            duration = self.get_effective_lock_duration()
            self.lock_expiration = now + timedelta(minutes=duration)
            self.logger.info(
                f"🔒 Zone '{self.name}' locked for {duration}m ({reason}). "
                f"Expires: {self.lock_expiration.strftime('%H:%M:%S')}"
                + (" — lock restarted" if restarted else "")
            )

    def extend_lock(self) -> bool:
        """Extend a duration lock by the extension value, within the maximum
        lock hours counted from when the lock was taken. Night-end locks are
        never extended. Returns True when the lock was extended."""
        if not self.locked or self.lock_rule != "duration":
            return False
        now = clock.now()
        extension = self.get_effective_lock_extension_duration()
        if extension <= 0:
            return False
        started = self.lock_started or now
        hard_stop = started + timedelta(hours=self.get_max_lock_hours())
        new_expiration = min(now + timedelta(minutes=extension), hard_stop)
        if new_expiration <= now:
            return False
        self.lock_expiration = new_expiration
        self.lock_extensions += 1
        self.logger.info(
            f"🔒 Zone '{self.name}' lock extended by {extension}m (presence detected). "
            f"Expires: {self.lock_expiration.strftime('%H:%M:%S')}"
        )
        return True

    def decide_at_expiry(self) -> str:
        """At (or after) lock_expiration: 'extend' or 'unlock'."""
        if not self.locked:
            return "unlock"
        if self.lock_rule == "duration" and self.has_presence_detected() and self.extend_lock():
            return "extend"
        return "unlock"

    def unlock_zone(self, reason: str = "expired") -> None:
        """Unlock the zone."""
        self.locked = False
        self.lock_expiration = None
        self.lock_rule = None
        self.lock_started = None
        self.lock_step = None
        self.lock_extensions = 0
        self.logger.info(f"🔓 Zone '{self.name}' unlocked ({reason})")

    def is_lock_expired(self) -> bool:
        """Check if the lock has expired."""
        if not self.locked or self.lock_expiration is None:
            return False
        return clock.now() >= self.lock_expiration

    def lock_state_dict(self) -> Optional[dict]:
        """Serializable lock state for the state store (None when unlocked)."""
        if not self.locked or self.lock_expiration is None:
            return None
        return {
            "until": self.lock_expiration.isoformat(timespec="seconds"),
            "rule": self.lock_rule,
            "reason": self.lock_reason,
            "started": (self.lock_started or clock.now()).isoformat(timespec="seconds"),
            "step": self.lock_step,
            "extensions": self.lock_extensions,
        }

    def restore_lock_state(self, state: dict) -> bool:
        """Re-establish a persisted lock. Returns False (and stays unlocked)
        when the stored lock has already expired or is unreadable."""
        try:
            until = datetime.fromisoformat(state["until"])
        except (KeyError, TypeError, ValueError):
            return False
        if until <= clock.now():
            return False
        self.locked = True
        self.lock_expiration = until
        self.lock_rule = state.get("rule") or "duration"
        self.lock_reason = state.get("reason") or "restored"
        try:
            self.lock_started = datetime.fromisoformat(state["started"])
        except (KeyError, TypeError, ValueError):
            self.lock_started = clock.now()
        self.lock_step = state.get("step")
        self.lock_extensions = int(state.get("extensions") or 0)
        self.logger.info(
            f"🔒 Zone '{self.name}' lock restored ({self.lock_rule}, {self.lock_reason}). "
            f"Expires: {until.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return True

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

    def is_self_triggered_change(self, grace_seconds: int = 10) -> bool:
        """Check if a fan speed change was likely caused by the plugin itself."""
        if self._last_speed_command_time is None:
            return False
        elapsed = (clock.now() - self._last_speed_command_time).total_seconds()
        return elapsed < grace_seconds

    def has_variable(self, var_id: int) -> bool:
        """Check if a variable ID is relevant to this zone.

        Checks all seasons' ideal temp var_ids, not just the current season,
        so we don't miss updates when seasons transition.
        """
        for season_cfg in self.seasonal_ideal_temp.values():
            if season_cfg.get("source") == "variable" and var_id == season_cfg.get("var_id"):
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
            except Exception as e:
                self._debug_log(f"Error getting state '{key}': {e}")
                continue

        try:
            dev.updateStatesOnServer(state_list)
        except Exception as e:
            self.logger.error(f"Failed to sync zone device '{self.name}': {e}")

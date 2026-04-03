import datetime
import logging
import threading
from typing import List, Optional

from .auto_fan_base import AutoFanBase
from .auto_fan_config import AutoFanConfig
from .fan_zone import FanZone
from .seasons import get_current_season
from .speed_curve import apply_modifiers, calculate_base_speed
from .speed_plan import SpeedPlan

try:
    import indigo
except ImportError:
    pass

# Device state keys that indicate a manual speed change on a fan
SPEED_CHANGE_KEYS = ["speedLevel", "brightness", "onState", "onOffState", "speedIndex"]


def _extract_value(source: dict, keys: tuple):
    """Return the first matching numeric value from source dict for the given keys."""
    for k in keys:
        if k in source:
            try:
                return float(source[k])
            except (ValueError, TypeError):
                return source[k]
    return None


def _format_change_line(change: dict) -> str:
    """Format a single device change record for the consolidated log."""
    role = change["role"]
    old = change.get("old_value")
    new = change.get("new_value")

    if role == "temperature":
        if old is not None and new is not None:
            return f"🌡️ Temperature: {old:.1f}°F → {new:.1f}°F"
        return f"🌡️ Temperature: {new:.1f}°F" if new else f"🌡️ {change['device_name']}: updated"

    if role == "humidity":
        if old is not None and new is not None:
            return f"💧 Humidity: {old:.0f}% → {new:.0f}%"
        return f"💧 Humidity: {new:.0f}%" if new else f"💧 {change['device_name']}: updated"

    if role == "presence":
        state = "detected" if new else "not detected"
        return f"👤 Presence: {state}"

    if role == "thermostat":
        parts = []
        for k, v in change.get("diff", {}).items():
            parts.append(f"{k}: {v}")
        return f"🏠 Thermostat: {', '.join(parts)}" if parts else f"🏠 Thermostat: updated"

    if role == "weather":
        if old is not None and new is not None:
            return f"🌤️ Outdoor temp: {old:.1f}°F → {new:.1f}°F"
        return f"🌤️ Outdoor temp: {new:.1f}°F" if new else f"🌤️ Weather: updated"

    return f"📡 {change['device_name']}: updated"


class AutoFanAgent(AutoFanBase):
    """
    Event router and zone processor for the Auto Fan plugin.

    Handles device/variable change events, routes them to the appropriate zones,
    manages lock timers, and coordinates fan speed changes.

    Sensor changes are debounced per zone: rapid device updates within a 1-second
    window are aggregated and processed once with a single consolidated log entry.
    Fan device changes (manual overrides) are always handled immediately.
    """

    def __init__(self, config: AutoFanConfig) -> None:
        super().__init__()
        self.config = config
        self._timers = {}

        # Debounce state: aggregate rapid sensor changes per zone
        self._pending_zone_changes = {}   # zone.name -> {dev_id: change_record}
        self._debounce_timers = {}        # zone.name -> threading.Timer

        # Give each zone a backreference to the agent
        for z in self.config.zones:
            z._config.agent = self

    def process_zone(self, zone: FanZone, trigger: Optional[str] = None,
                     pending_changes: Optional[list] = None) -> bool:
        """
        Main automation function for a single fan zone.

        Calculates target speed and applies changes if needed.

        Args:
            zone: The fan zone to process.
            trigger: Description of what triggered this evaluation
                (e.g., "device 'Bedroom Temp'").
            pending_changes: Aggregated device change records from debouncing.
                When present, uses consolidated multi-line log format.

        Returns:
            True if the zone was evaluated successfully, False if skipped.
        """
        # GUARD: plugin globally disabled
        if not self.config.enabled:
            self._debug_log(
                f"Skipping process_zone: plugin globally DISABLED"
            )
            return False

        # GUARD: zone disabled
        if not zone.enabled:
            self._debug_log(f"Skipping process_zone for '{zone.name}' — zone disabled")
            return False

        # GUARD: zone locked
        if zone.locked:
            if zone.is_lock_expired():
                zone.unlock_zone()
            else:
                self._debug_log(
                    f"Zone '{zone.name}' is locked until {zone.lock_expiration}"
                )
                return False

        # Calculate target speed
        plan = zone.calculate_target_speed()

        # Check for exclusions (missing data, disabled, etc.)
        if plan.exclusions:
            if self.config.log_non_events:
                for emoji, msg in plan.exclusions:
                    self.logger.info(f"\t{emoji} {msg}")
            zone.sync_indigo_device()
            return False

        # Apply changes if speed differs
        if zone.has_speed_change():
            self._log_speed_change(zone, plan, trigger=trigger,
                                   pending_changes=pending_changes)
            zone.apply_speed_change()
        else:
            self._debug_log(f"Zone '{zone.name}': no speed change needed")

        zone.sync_indigo_device()
        return True

    def process_device_change(self, orig_dev, diff: dict) -> List[FanZone]:
        """
        Process a device change event and route to relevant zones.

        If the changed device is a fan device, detect manual changes and lock the zone.
        If it's a sensor/thermostat, reprocess the zone.

        Returns:
            List of zones that were processed.
        """
        processed = []
        for zone in self.config.zones:
            if not zone.has_device(orig_dev.id):
                continue

            if zone.is_fan_device(orig_dev.id):
                # Fan device changed — check for manual change (lock trigger)
                if not zone.enabled:
                    if self.config.log_non_events:
                        self.logger.info(
                            f"🚫 Ignored fan change from '{orig_dev.name}' for disabled zone '{zone.name}'"
                        )
                    continue

                # Detect manual speed change (not caused by us)
                if any(k in diff for k in SPEED_CHANGE_KEYS) and not zone.locked:
                    if zone.is_self_triggered_change():
                        self._debug_log(
                            f"Ignoring self-triggered fan change on '{orig_dev.name}' for zone '{zone.name}'"
                        )
                        processed.append(zone)
                        continue
                    zone.lock_zone(reason=f"manual change on '{orig_dev.name}'")
                    processed.append(zone)

                    # Schedule lock expiration check
                    if zone.lock_expiration:
                        delay = (
                            zone.lock_expiration
                            + datetime.timedelta(seconds=2)
                            - datetime.datetime.now()
                        ).total_seconds()
                        if delay > 0:
                            if zone.name in self._timers:
                                self._timers[zone.name].cancel()
                            timer = threading.Timer(
                                delay, self._process_expired_lock, args=[zone]
                            )
                            timer.daemon = True
                            self._timers[zone.name] = timer
                            timer.start()
            else:
                # Sensor, thermostat, presence, humidity, or weather change —
                # queue for debounced processing to aggregate rapid updates
                self._queue_zone_change(zone, orig_dev, diff)

        return processed

    def process_variable_change(self, orig_var, new_var) -> List[FanZone]:
        """
        Process a variable change event.

        If it's the global season variable, reprocess all zones.
        If it's a zone-level variable (e.g., ideal temp), process that zone.

        Returns:
            List of zones that were processed.
        """
        processed = []

        # Global season variable affects all zones
        if (self.config.season_detection_mode == "variable"
                and orig_var.id == self.config.season_var_id):
            for zone in self.config.zones:
                if self.process_zone(zone, trigger="season variable change"):
                    processed.append(zone)
            return processed

        for zone in self.config.zones:
            if zone.has_variable(orig_var.id):
                if self.process_zone(zone, trigger=f"variable '{new_var.name}'"):
                    processed.append(zone)

        return processed

    def process_all_zones(self, trigger: Optional[str] = None) -> None:
        """Process all zones (used after global config changes)."""
        for zone in self.config.zones:
            self.process_zone(zone, trigger=trigger)

    def _process_expired_lock(self, zone: FanZone) -> None:
        """
        Called by timer when a zone's lock should have expired.
        If unlocked, process the zone. If still locked, reschedule.

        Lock timer lifecycle: timers fire 2s after lock_expiration. If the lock
        was extended (e.g., by ongoing presence), the timer finds the lock still
        active and reschedules itself for the new expiration. This repeats until
        the lock truly expires, at which point automation resumes.
        """
        self._debug_log(
            f"_process_expired_lock for '{zone.name}', locked={zone.locked}"
        )
        if zone.locked and zone.is_lock_expired():
            zone.unlock_zone()

        if not zone.locked:
            if zone.name in self._timers:
                self._timers[zone.name].cancel()
                del self._timers[zone.name]
            self.process_zone(zone, trigger="lock expired")
        else:
            # Still locked (extended), reschedule
            if zone.lock_expiration:
                delay = (zone.lock_expiration - datetime.datetime.now()).total_seconds()
                if delay > 0:
                    if zone.name in self._timers:
                        self._timers[zone.name].cancel()
                    timer = threading.Timer(
                        delay + 2, self._process_expired_lock, args=[zone]
                    )
                    timer.daemon = True
                    self._timers[zone.name] = timer
                    timer.start()

    # ---- Debounced Zone Processing ----

    def _queue_zone_change(self, zone: FanZone, orig_dev, diff: dict) -> None:
        """Queue a sensor change for debounced processing.

        Aggregates rapid device updates within a 1-second window so the zone
        is processed once with a single consolidated log entry instead of once
        per device.
        """
        zone_name = zone.name
        change = self._classify_device_change(zone, orig_dev, diff)

        if zone_name not in self._pending_zone_changes:
            self._pending_zone_changes[zone_name] = {}

        # If same device updated again, keep original old_value, update new_value
        existing = self._pending_zone_changes[zone_name].get(orig_dev.id)
        if existing:
            change["old_value"] = existing["old_value"]

        self._pending_zone_changes[zone_name][orig_dev.id] = change

        # Reset debounce timer (1-second window)
        if zone_name in self._debounce_timers:
            self._debounce_timers[zone_name].cancel()

        timer = threading.Timer(1.0, self._process_debounced_zone, args=[zone])
        timer.daemon = True
        self._debounce_timers[zone_name] = timer
        timer.start()

    def _classify_device_change(self, zone: FanZone, orig_dev, diff: dict) -> dict:
        """Classify a device change by its role in the zone and extract old/new values."""
        dev_id = orig_dev.id
        record = {"device_name": orig_dev.name, "device_id": dev_id,
                  "role": "unknown", "old_value": None, "new_value": None}

        temp_keys = ("sensorValue", "temperature", "temp")
        humidity_keys = ("sensorValue", "humidity", "relativeHumidity")
        weather_keys = ("feelslike", "temp", "temperature", "sensorValue")
        presence_keys = ("onState", "onOffState")

        if dev_id in zone.temp_sensor_dev_ids:
            record["role"] = "temperature"
            record["old_value"] = _extract_value(orig_dev.states, temp_keys)
            record["new_value"] = _extract_value(diff, temp_keys) or record["old_value"]

        elif dev_id in zone.humidity_dev_ids:
            record["role"] = "humidity"
            record["old_value"] = _extract_value(orig_dev.states, humidity_keys)
            record["new_value"] = _extract_value(diff, humidity_keys) or record["old_value"]

        elif dev_id in zone.presence_dev_ids:
            record["role"] = "presence"
            # Presence uses boolean values — extract raw, don't coerce to float
            for k in presence_keys:
                if k in orig_dev.states:
                    record["old_value"] = bool(orig_dev.states[k])
                    break
            for k in presence_keys:
                if k in diff:
                    record["new_value"] = bool(diff[k])
                    break

        elif dev_id == zone.thermostat_dev_id:
            record["role"] = "thermostat"
            record["diff"] = {k: v for k, v in diff.items()
                              if k in ("heatSetpoint", "coolSetpoint", "hvacOperationMode")}

        elif dev_id == getattr(self.config, "weather_dev_id", None):
            record["role"] = "weather"
            record["old_value"] = _extract_value(orig_dev.states, weather_keys)
            record["new_value"] = _extract_value(diff, weather_keys) or record["old_value"]

        return record

    def _process_debounced_zone(self, zone: FanZone) -> None:
        """Process a zone after its debounce window closes.

        Evaluates the zone once using current sensor data and logs a single
        consolidated entry listing all device changes from the debounce window.
        """
        zone_name = zone.name
        pending = self._pending_zone_changes.pop(zone_name, {})
        self._debounce_timers.pop(zone_name, None)

        self.process_zone(zone, pending_changes=list(pending.values()))

    def _log_speed_change(self, zone: FanZone, plan: SpeedPlan,
                          trigger: Optional[str] = None,
                          pending_changes: Optional[list] = None) -> None:
        """Log structured multi-line speed change at INFO level."""
        from_str, to_str = zone.get_speed_change_description()
        self.logger.info(f"🌀 Zone '{zone.name}': fan speed {from_str} → {to_str}")

        # Triggers
        if pending_changes:
            for change in pending_changes:
                self.logger.info(f"\t🔄 {_format_change_line(change)}")
        elif trigger:
            self.logger.info(f"\t🔄 Triggered by: {trigger}")

        # Calculation
        if plan.contributions:
            self.logger.info("\t📝 Calculation:")
            for emoji, msg in plan.contributions:
                self.logger.info(f"\t\t{emoji} {msg}")

        # Changes
        if plan.device_changes:
            self.logger.info("\t⚙️ Changes:")
            for emoji, msg in plan.device_changes:
                self.logger.info(f"\t\t{emoji} {msg}")

        if self.logger.isEnabledFor(logging.DEBUG):
            season = get_current_season(**self.config.get_season_kwargs())
            self._log_zone_breakdown_full(zone, season, self.logger.debug)

    def get_zones(self) -> List[FanZone]:
        return self.config.zones

    def reset_locks(self, zone_name: str = None, reason: str = "manual reset") -> None:
        """Reset locks for a specific zone or all zones."""
        targets = [
            z for z in self.config.zones
            if z.locked and (zone_name is None or z.name == zone_name)
        ]
        for zone in targets:
            zone.unlock_zone()
            self.process_zone(zone, trigger="manual unlock")
            if zone.name in self._timers:
                self._timers[zone.name].cancel()
                del self._timers[zone.name]

    def print_locked_zones(self) -> None:
        """Log information about all currently locked zones."""
        locked_zones = [z for z in self.config.zones if z.locked]
        if not locked_zones:
            self.logger.info("No locked zones.")
        else:
            self.logger.info("🔒 Locked Zones:")
            for zone in locked_zones:
                exp = zone.lock_expiration.strftime('%H:%M:%S') if zone.lock_expiration else "N/A"
                self.logger.info(f"🔒 Zone '{zone.name}' locked until {exp}")

    def _log_zone_breakdown_full(self, zone: FanZone, season: str, log_fn) -> None:
        """Log detailed temperature/speed breakdown using the provided log function."""
        log = log_fn
        log(f"🌀 Zone '{zone.name}' — Detailed Breakdown")

        # Ideal temperature
        log("\t🌡️ Ideal Temperature:")
        for emoji, msg in zone.get_ideal_temperature_breakdown():
            prefix = f"{emoji} " if emoji else ""
            log(f"\t\t{prefix}{msg}")

        # Sensor readings
        readings = zone.get_sensor_readings()
        if readings:
            log("\t🌡️ Sensor Readings:")
            for dev_id, name, value in readings:
                if value is not None:
                    log(f"\t\t'{name}' (id:{dev_id}): {value:.1f}°F")
                else:
                    log(f"\t\t'{name}' (id:{dev_id}): unavailable")
            avg = zone.get_current_temperature()
            if avg is not None:
                log(f"\t\tAverage: {avg:.1f}°F")
        else:
            log("\tNo temperature sensors configured")

        # Delta
        delta = zone.get_temperature_delta()
        if delta is not None:
            if delta > 0:
                interp = "warmer than ideal"
            elif delta < 0:
                interp = "cooler than ideal"
            else:
                interp = "at ideal"
            log(f"\t📊 Delta: {delta:+.1f}°F ({interp})")
        else:
            log("\t📊 Delta: unavailable (missing sensor data)")
            return

        # Season and fan curve
        curve = zone.fan_curve
        temp_range = curve.get("temperature_range", "?")
        num_points = len(curve.get("points", []))
        log(f"\t📈 Fan curve: {season}, range: ±{temp_range}°F, {num_points} points")

        # Speed calculation
        base_speed = calculate_base_speed(delta, curve)
        final_speed, modifier_contribs = apply_modifiers(
            base_speed=base_speed,
            modifiers=zone.modifiers,
            is_hvac_cooling=zone.is_hvac_cooling(),
            is_hvac_heating=zone.is_hvac_heating(),
            humidity=zone.get_humidity(),
            is_home=self.config.is_home(),
            season=season,
        )

        log("\tTarget speed calculation:")
        log(f"\t\ttarget on fan speed curve: {base_speed:.1f}%")
        if modifier_contribs:
            total_delta = final_speed - base_speed
            log(f"\t\tmodifiers: {base_speed:.1f}% -> {final_speed:.1f}%; total: {total_delta:+.1f}%")
            for emoji, msg in modifier_contribs:
                log(f"\t\t\t{emoji} {msg}")
        else:
            log("\t\tmodifiers: none active")

        info = zone._get_device_speed_info()
        if info.get("speed_index_count"):
            count = info["speed_index_count"]
            target_idx = zone._pct_to_speed_index(final_speed, count)
            target_pct = zone._speed_index_to_pct(target_idx, count)
            log(f"\t\tfinal target: {target_pct}% (speed index {target_idx}/{count - 1})")
        else:
            log(f"\t\tfinal target: {round(final_speed)}%")

        from_str, _ = zone.get_speed_change_description()
        log(f"\tCurrent fan speed is {from_str}")

        # Status notes
        if not zone.enabled:
            log("\t⏸️ Zone is DISABLED")
        if zone.locked:
            exp = zone.lock_expiration.strftime('%H:%M:%S') if zone.lock_expiration else "N/A"
            log(f"\t🔒 Zone is LOCKED until {exp}")

    def print_zone_breakdowns(self) -> None:
        """Log detailed temperature breakdown for all zones (always at INFO)."""
        zones = self.config.zones
        if not zones:
            self.logger.info("No zones configured.")
            return
        for zone in zones:
            season = get_current_season(**self.config.get_season_kwargs())
            self._log_zone_breakdown_full(zone, season, self.logger.info)

    def enable_all_zones(self) -> None:
        self.config.enabled = True

    def disable_all_zones(self) -> None:
        self.config.enabled = False

    def enable_zone(self, zone_name: str) -> None:
        for zone in self.config.zones:
            if zone.name == zone_name:
                zone.enabled = True
                break

    def disable_zone(self, zone_name: str) -> None:
        for zone in self.config.zones:
            if zone.name == zone_name:
                zone.enabled = False
                break

    def refresh_all_indigo_devices(self) -> None:
        """Refresh all Indigo device states and clean up stale devices."""
        for zone in self.config.zones:
            zone.sync_indigo_device()

        # Clean up stale zone devices
        active_indices = {zone.zone_index for zone in self.config.zones}
        for dev in indigo.devices:
            if (
                dev.pluginId == "com.vtmikel.autofan"
                and dev.deviceTypeId == "auto_fan_zone"
            ):
                idx = int(dev.pluginProps.get("zone_index", -1))
                if idx not in active_indices:
                    try:
                        indigo.device.delete(dev.id)
                        self.logger.info(
                            f"Deleted stale zone device: {dev.name} (index: {idx})"
                        )
                    except Exception as e:
                        self.logger.error(
                            f"Failed to delete stale zone device {dev.name}: {e}"
                        )

    def refresh_indigo_device(self, dev_id: int) -> None:
        """Refresh a specific Indigo device's state."""
        for zone in self.config.zones:
            if zone.indigo_dev and zone.indigo_dev.id == dev_id:
                zone.sync_indigo_device()

    def shutdown(self) -> None:
        """Cancel all outstanding timers."""
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        for t in self._debounce_timers.values():
            t.cancel()
        self._debounce_timers.clear()
        self._pending_zone_changes.clear()

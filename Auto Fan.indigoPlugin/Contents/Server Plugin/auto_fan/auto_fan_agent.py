import datetime
import threading
from typing import List

from .auto_fan_base import AutoFanBase
from .auto_fan_config import AutoFanConfig
from .fan_zone import FanZone

try:
    import indigo
except ImportError:
    pass

# Device state keys that indicate a manual speed change on a fan
SPEED_CHANGE_KEYS = ["speedLevel", "brightness", "onState", "onOffState", "speedIndex"]


class AutoFanAgent(AutoFanBase):
    """
    Event router and zone processor for the Auto Fan plugin.

    Handles device/variable change events, routes them to the appropriate zones,
    manages lock timers, and coordinates fan speed changes.
    """

    def __init__(self, config: AutoFanConfig) -> None:
        super().__init__()
        self.config = config
        self._timers = {}

        # Give each zone a backreference to the agent
        for z in self.config.zones:
            z._config.agent = self

    def process_zone(self, zone: FanZone) -> bool:
        """
        Main automation function for a single fan zone.

        Calculates target speed and applies changes if needed.

        Returns:
            True if changes were applied, False otherwise.
        """
        # Sync device state
        zone.sync_indigo_device()

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
            self.logger.info(f"🌀 Zone '{zone.name}': adjusting fan speed")
            self.logger.info(f"\t📝 Speed logic:")
            for emoji, msg in plan.contributions:
                self.logger.info(f"\t\t{emoji} {msg}")
            self.logger.info(f"\t⚙️ Changes:")
            for emoji, msg in plan.device_changes:
                self.logger.info(f"\t\t{emoji} {msg}")
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
                # Sensor, thermostat, presence, humidity, or weather change
                if self.process_zone(zone):
                    processed.append(zone)

        return processed

    def process_variable_change(self, orig_var, new_var) -> List[FanZone]:
        """
        Process a variable change event.

        If it's a global behavior variable, process all zones.
        If it's a zone-level variable (e.g., ideal temp), process that zone.

        Returns:
            List of zones that were processed.
        """
        processed = []

        if self.config.has_variable(orig_var.id):
            self.logger.debug(
                f"Global config has variable {orig_var.id}; running process_all_zones"
            )
            self.process_all_zones()
            return list(self.config.zones)

        for zone in self.config.zones:
            if zone.has_variable(orig_var.id):
                if self.process_zone(zone):
                    processed.append(zone)

        return processed

    def process_all_zones(self) -> None:
        """Process all zones (used after global config changes)."""
        for zone in self.config.zones:
            self.process_zone(zone)

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
            self.process_zone(zone)
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
            self.process_zone(zone)
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

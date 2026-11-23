import datetime
import logging
import threading
from typing import List, Optional

from . import clock
from .auto_fan_base import AutoFanBase
from .auto_fan_config import AutoFanConfig
from .fan_zone import FanZone
from .learning import Recommender
from .override_log import OverrideLog
from .seasons import get_current_season
from .speed_curve import apply_modifiers, calculate_base_speed
from .speed_plan import SpeedPlan

try:
    import indigo
except ImportError:
    pass

# Device state keys that indicate a manual speed change on a fan
SPEED_CHANGE_KEYS = ["speedLevel", "brightness", "onState", "onOffState", "speedIndex", "baf_speed"]

# The lock-expiry timer fires this long after lock_expiration so the
# extend-or-unlock decision is always made at/after the true expiration.
LOCK_EXPIRY_GRACE_SECONDS = 2

# A temporary offset made at night ends at the same moment as the night lock.
# Its timer fires a few seconds after the lock's, so the lock's evaluation
# (which also ends the offset) runs first and the fan is commanded once.
OFFSET_EXPIRY_GRACE_SECONDS = LOCK_EXPIRY_GRACE_SECONDS + 3

# One adjustment often reaches the plugin as several fan reports a few
# seconds apart (0% → 43% → 57%). The lock is taken at the first report; the
# change is recorded, and its temporary offset derived, once the fan has
# stayed put this long.
MANUAL_SETTLE_SECONDS = 30


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
        self._offset_timers = {}
        self._learning_timer = None
        # Manual changes waiting for the fan to settle: zone.name -> (timer, fan_dev, readjust)
        self._settle_lock = threading.Lock()
        self._settle_timers = {}
        # Set by the plugin: re-reads the config file (used after applying a recommendation)
        self.reload_config_callback = None

        # Debounce state: aggregate rapid sensor changes per zone
        self._debounce_lock = threading.Lock()
        self._pending_zone_changes = {}   # zone.name -> {dev_id: change_record}
        self._debounce_timers = {}        # zone.name -> threading.Timer

        # Give each zone a backreference to the agent
        for z in self.config.zones:
            z._config.agent = self

        # The override record (manual changes + lock releases), kept 400 days
        self.override_log = OverrideLog(config.state_store)

        # Locks and temporary comfort offsets survive restarts and config reloads
        self._restore_locks()
        self._restore_offsets()

        # Comfort learning: recommendations from the override record
        self.recommender = Recommender(config, self.override_log, config.state_store)
        edited = self.recommender.note_hand_edits(self.config.zones)
        for key in edited:
            self._debug_log(f"Learnable setting changed by hand: {key}")
        self._schedule_learning_run()

    def process_zone(self, zone: FanZone, trigger: Optional[str] = None,
                     pending_changes: Optional[list] = None,
                     released: Optional[dict] = None) -> bool:
        """
        Main automation function for a single fan zone.

        Calculates target speed and applies changes if needed.

        Args:
            zone: The fan zone to process.
            trigger: Description of what triggered this evaluation
                (e.g., "device 'Bedroom Temp'").
            pending_changes: Aggregated device change records from debouncing.
                When present, uses consolidated multi-line log format.
            released: Set when a lock was just released (by the caller or by
                the guard below): {"reason", "rule", "started"}. The release is
                written to the override record with the speed automation
                resumes at.

        Returns:
            True if the zone was evaluated successfully, False if skipped.
        """
        # GUARD: plugin globally disabled
        if not self.config.enabled:
            self._debug_log(
                f"Skipping process_zone: plugin globally DISABLED"
            )
            return False

        # Comfort learning's watch counts only days the house was home
        try:
            self.recommender.note_home(clock.now(), self.config.is_home())
        except Exception as e:
            self._debug_log(f"Could not note presence for comfort learning: {e}")

        # GUARD: zone disabled
        if not zone.enabled:
            self._debug_log(f"Skipping process_zone for '{zone.name}' — zone disabled")
            return False

        # GUARD: zone locked
        if zone.locked:
            if not self.config.is_home():
                # Nobody home: a manual change no longer needs protecting
                released = self._release_lock(zone, "house is away")
            elif zone.is_lock_expired():
                # Normally the timer handles expiry; a late evaluation gets
                # the same extend-or-unlock decision.
                if zone.decide_at_expiry() == "extend":
                    self._persist_locks()
                    self._schedule_lock_check(zone)
                    return False
                released = self._release_lock(
                    zone, "night ended" if zone.lock_rule == "night_end" else "expired"
                )
            else:
                self._debug_log(
                    f"Zone '{zone.name}' is locked until {zone.lock_expiration}"
                )
                return False

        # Calculate target speed
        plan = zone.calculate_target_speed()

        if released:
            resumed = plan.target_speed_pct if not plan.exclusions else None
            self.override_log.record_release(
                zone, released["reason"], released["rule"], released["started"],
                resumed, zone._step_for_pct(resumed) if resumed is not None else None,
            )

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
            zone.acknowledge_current_step()
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
                # Fan device changed — decide whether a person did it
                if not zone.enabled:
                    if self.config.log_non_events:
                        self.logger.info(
                            f"🚫 Ignored fan change from '{orig_dev.name}' for disabled zone '{zone.name}'"
                        )
                    continue
                if not any(k in diff for k in SPEED_CHANGE_KEYS):
                    continue
                if zone.is_change_from_plugin():
                    self._debug_log(
                        f"Fan '{orig_dev.name}' settled on the plugin's own step "
                        f"{zone.current_step()}; not a manual change for zone '{zone.name}'"
                    )
                    processed.append(zone)
                    continue
                if zone.locked:
                    if zone.lock_step is not None and zone.current_step() == zone.lock_step:
                        self._debug_log(
                            f"Fan '{orig_dev.name}' reported the step the lock was taken on; "
                            f"zone '{zone.name}' stays locked as is"
                        )
                        continue
                    # A further manual change during a lock restarts the lock
                    self.on_manual_change(zone, orig_dev, readjust=True, settle_seconds=MANUAL_SETTLE_SECONDS)
                else:
                    self.on_manual_change(zone, orig_dev, readjust=False, settle_seconds=MANUAL_SETTLE_SECONDS)
                processed.append(zone)
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

        # Home/away variable affects every zone (away cap, and locks release)
        if self.config.away_var_id and orig_var.id == self.config.away_var_id:
            for zone in self.config.zones:
                if self.process_zone(zone, trigger=f"home/away variable '{new_var.name}'"):
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

    # ---- Lock lifecycle ----

    def on_manual_change(self, zone: FanZone, fan_dev, readjust: bool = False,
                         settle_seconds: float = 0.0) -> None:
        """A person changed the fan: lock (or restart the lock), persist, arm the timer.

        The change is recorded and its temporary offset derived at once, or,
        with ``settle_seconds``, once no further report has arrived for that
        long; the reports of one adjustment are then recorded as one change,
        at the step the fan settled on.
        """
        reason = (
            f"manual re-adjustment on '{fan_dev.name}'" if readjust
            else f"manual change on '{fan_dev.name}'"
        )
        zone.lock_zone(reason=reason)
        self._persist_locks()
        self._schedule_lock_check(zone)
        if settle_seconds <= 0:
            self._record_manual_change(zone, fan_dev, readjust)
            return
        with self._settle_lock:
            pending = self._settle_timers.pop(zone.name, None)
            if pending:
                pending[0].cancel()
                readjust = pending[2]  # the adjustment began as a change or a re-adjustment
            timer = threading.Timer(settle_seconds, self._settled_manual_change, args=[zone])
            timer.daemon = True
            self._settle_timers[zone.name] = (timer, fan_dev, readjust)
            timer.start()

    def _settled_manual_change(self, zone: FanZone) -> None:
        with self._settle_lock:
            pending = self._settle_timers.pop(zone.name, None)
        if pending is None:
            return  # already flushed (lock released, shutdown)
        self._record_manual_change(zone, pending[1], pending[2])

    def flush_settling_changes(self, zone_name: Optional[str] = None) -> None:
        """Record now the manual changes still waiting for the fan to settle
        (one zone, or all). Called before a lock is released and at shutdown,
        so the change is recorded at the step the person chose."""
        with self._settle_lock:
            names = [zone_name] if zone_name else list(self._settle_timers)
            pending = [(n, self._settle_timers.pop(n)) for n in names if n in self._settle_timers]
        for name, (timer, fan_dev, readjust) in pending:
            timer.cancel()
            zone = next((z for z in self.config.zones if z.name == name), None)
            if zone is not None:
                self._record_manual_change(zone, fan_dev, readjust)

    def _record_manual_change(self, zone: FanZone, fan_dev, readjust: bool) -> None:
        """Write the override record and derive the temporary offset from the
        speed the fan is on now."""
        human_pct = zone._get_current_speed_pct()
        # What the change says about the ideal: hold it until the night window
        # ends (change at night) or begins (change by day).
        derived = zone.derive_temporary_offset(human_pct)
        self.override_log.record_override(
            zone, fan_dev.name, readjust,
            plugin_step=zone._last_commanded_step, plugin_pct=zone._target_speed_pct,
            human_step=zone.current_step() if zone.fan_dev_id else zone.lock_step, human_pct=human_pct,
            implied_ideal=derived[0] if derived else None,
            implied_offset=derived[1] if derived else None,
        )
        if derived is not None:
            implied_ideal, offset, expires = derived
            if offset:
                zone.set_temporary_offset(offset, expires, "manual change", implied_ideal)
            else:
                zone.clear_temporary_offset("manual change implies no offset")
            self._persist_offsets()
            self._schedule_offset_check(zone)

    def _release_lock(self, zone: FanZone, reason: str) -> dict:
        """Unlock, stop the timer, persist; return the release facts for the record."""
        self.flush_settling_changes(zone.name)
        facts = {"reason": reason, "rule": zone.lock_rule, "started": zone.lock_started}
        zone.unlock_zone(reason)
        self._cancel_lock_timer(zone)
        self._persist_locks()
        return facts

    def _cancel_lock_timer(self, zone: FanZone) -> None:
        timer = self._timers.pop(zone.name, None)
        if timer:
            timer.cancel()

    def _schedule_lock_check(self, zone: FanZone) -> None:
        """(Re)arm the single lock-expiry timer for a zone, a little after its
        lock_expiration. Safe to call on lock creation and on every extension."""
        self._cancel_lock_timer(zone)
        if not zone.locked or zone.lock_expiration is None:
            return
        delay = max(0.0, (zone.lock_expiration - clock.now()).total_seconds()) + LOCK_EXPIRY_GRACE_SECONDS
        timer = threading.Timer(delay, self.process_expired_lock, args=[zone])
        timer.daemon = True
        self._timers[zone.name] = timer
        timer.start()

    def process_expired_lock(self, zone: FanZone) -> None:
        """Single authority for lock expiry: extend (duration lock, presence
        detected, under the maximum) or unlock and resume automation."""
        if zone.name not in self._timers:
            return  # cancelled (reset, reload) — do not resurrect
        if not zone.locked:
            self._timers.pop(zone.name, None)
            return
        if not zone.is_lock_expired():
            self._schedule_lock_check(zone)  # fired early; re-arm
            return
        if zone.decide_at_expiry() == "extend":
            self._persist_locks()
            self._schedule_lock_check(zone)
            return
        released = self._release_lock(
            zone, "night ended" if zone.lock_rule == "night_end" else "expired"
        )
        self.process_zone(zone, trigger="lock expired", released=released)

    def _persist_locks(self) -> None:
        store = getattr(self.config, "state_store", None)
        if store is None:
            return
        store.save("locks", {
            z.name: z.lock_state_dict() for z in self.config.zones if z.locked and z.lock_state_dict()
        })

    def _restore_locks(self) -> None:
        store = getattr(self.config, "state_store", None)
        if store is None:
            return
        saved = store.load("locks", {}) or {}
        restored = 0
        for zone in self.config.zones:
            state = saved.get(zone.name)
            if isinstance(state, dict) and zone.restore_lock_state(state):
                self._schedule_lock_check(zone)
                restored += 1
        if saved:
            self._persist_locks()  # drops entries that expired while we were down
        if restored:
            self.logger.info(f"🔒 Restored {restored} lock(s) from the previous run")

    # ---- Comfort offsets ----

    UNTIL_CHOICES = ("night_end", "night_start", "daily_reset", "hours")

    def _until_to_datetime(self, zone: FanZone, until: str, hours: Optional[float] = None) -> datetime.datetime:
        now = clock.now()
        if until == "night_end":
            return zone.next_night_end(now)
        if until == "night_start":
            return zone.next_night_start(now)
        if until == "hours":
            return now + datetime.timedelta(hours=float(hours or 1))
        # daily_reset
        reset_hour = int(getattr(self.config, "daily_reset_hour", 10))
        candidate = now.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += datetime.timedelta(days=1)
        return candidate

    def set_comfort_offset(self, zone_name: str, offset: float, until: str = "daily_reset",
                           hours: Optional[float] = None, source: str = "action") -> bool:
        """Set a temporary comfort offset on a zone by hand (web page, Indigo
        action, Siri through an action group). Returns False for an unknown zone."""
        from .comfort import round_offset
        zone = next((z for z in self.config.zones if z.name == zone_name), None)
        if zone is None:
            self.logger.error(f"set_comfort_offset: no zone named '{zone_name}'")
            return False
        if until not in self.UNTIL_CHOICES:
            until = "daily_reset"
        offset = round_offset(float(offset))
        if offset == 0:
            return self.clear_comfort_offset(zone_name, source)
        zone.set_temporary_offset(offset, self._until_to_datetime(zone, until, hours), source)
        self._persist_offsets()
        self._schedule_offset_check(zone)
        self.process_zone(zone, trigger=f"comfort offset set ({source})")
        return True

    def clear_comfort_offset(self, zone_name: str, source: str = "action") -> bool:
        zone = next((z for z in self.config.zones if z.name == zone_name), None)
        if zone is None:
            self.logger.error(f"clear_comfort_offset: no zone named '{zone_name}'")
            return False
        zone.clear_temporary_offset(f"cleared by {source}")
        self._persist_offsets()
        self._cancel_offset_timer(zone)
        self.process_zone(zone, trigger=f"comfort offset cleared ({source})")
        return True

    def _cancel_offset_timer(self, zone: FanZone) -> None:
        timer = self._offset_timers.pop(zone.name, None)
        if timer:
            timer.cancel()

    def _schedule_offset_check(self, zone: FanZone) -> None:
        self._cancel_offset_timer(zone)
        if zone.temporary_offset is None or zone.temporary_offset_expires is None:
            return
        delay = max(0.0, (zone.temporary_offset_expires - clock.now()).total_seconds()) + OFFSET_EXPIRY_GRACE_SECONDS
        timer = threading.Timer(delay, self.process_offset_expiry, args=[zone])
        timer.daemon = True
        self._offset_timers[zone.name] = timer
        timer.start()

    def process_offset_expiry(self, zone: FanZone) -> None:
        if zone.name not in self._offset_timers:
            return
        self._offset_timers.pop(zone.name, None)
        if zone.temporary_offset is None:
            # Already ended by another evaluation (usually the night lock's
            # release); drop it from the saved state too.
            self._persist_offsets()
            return
        if zone.temporary_offset_active():
            self._schedule_offset_check(zone)  # fired early; re-arm
            return
        # temporary_offset_active() cleared the expired offset
        self._persist_offsets()
        self.process_zone(zone, trigger="comfort offset expired")

    def _persist_offsets(self) -> None:
        store = getattr(self.config, "state_store", None)
        if store is None:
            return
        store.save("offsets", {
            z.name: z.offset_state_dict() for z in self.config.zones if z.offset_state_dict()
        })

    def _restore_offsets(self) -> None:
        store = getattr(self.config, "state_store", None)
        if store is None:
            return
        saved = store.load("offsets", {}) or {}
        restored = 0
        for zone in self.config.zones:
            state = saved.get(zone.name)
            if isinstance(state, dict) and zone.restore_offset_state(state):
                self._schedule_offset_check(zone)
                restored += 1
        if saved:
            self._persist_offsets()
        if restored:
            self.logger.info(f"🎚️ Restored {restored} temporary comfort offset(s) from the previous run")

    def zone_status(self, zone: FanZone) -> dict:
        """Live status for the web editor's zone panel and the zones list."""
        return {
            "locked": zone.locked,
            "lock_rule": zone.lock_rule,
            "lock_reason": zone.lock_reason if zone.locked else "",
            "lock_expiration": zone.lock_expiration.strftime("%a %H:%M") if zone.locked and zone.lock_expiration else "",
            "temporary_offset": zone.temporary_offset if zone.temporary_offset_active() else None,
            "temporary_offset_expires": (zone.temporary_offset_expires.strftime("%a %H:%M")
                                         if zone.temporary_offset_active() and zone.temporary_offset_expires else ""),
            "temporary_offset_source": zone.temporary_offset_source if zone.temporary_offset_active() else "",
            "standing_offset": zone.standing_offset_value(),
            "configured_ideal": zone.get_configured_ideal_temperature(),
            "effective_ideal": zone.get_ideal_temperature(),
            "current_temperature": zone.get_current_temperature(),
            "current_speed_pct": zone._get_current_speed_pct(),
            "target_speed_pct": zone._target_speed_pct,
            "is_night": zone.is_night(),
            "recommendations": self.recommender.open_for_zone(zone.name),
            "open_recommendations": len(self.recommender.open_for_zone(zone.name)),
        }

    # ---- Comfort learning ----

    def _schedule_learning_run(self, after_run: bool = False) -> None:
        """Arm the daily learning run at the Daily Reset Hour."""
        if self._learning_timer:
            self._learning_timer.cancel()
        now = clock.now()
        reset_hour = int(getattr(self.config, "daily_reset_hour", 10))
        when = now.replace(hour=reset_hour, minute=0, second=30, microsecond=0)
        # A timer can fire a little early by the wall clock (seen: 2 s); right
        # after a run, a target still ahead today is the run that just happened.
        if when <= now or (after_run and when - now < datetime.timedelta(hours=1)):
            when += datetime.timedelta(days=1)
        delay = max(1.0, (when - now).total_seconds())
        timer = threading.Timer(delay, self._learning_tick)
        timer.daemon = True
        self._learning_timer = timer
        timer.start()

    def _learning_tick(self) -> None:
        try:
            self.run_learning()
        finally:
            self._schedule_learning_run(after_run=True)

    def run_learning(self) -> list:
        """Run the recommender now; log what is new; refresh zone devices."""
        try:
            new = self.recommender.run()
        except Exception as e:
            self.logger.exception(f"Comfort learning failed: {e}")
            return []
        for rec in new:
            self.logger.info(f"💡 Recommendation (beta) — {rec['title']}")
            self.logger.info(f"\t{rec['text']}")
            self.logger.info(f"\tProposed: {rec['setting_label']} {rec['current_label']} → {rec['proposed_label']} "
                             f"(apply or dismiss on the zone's page)")
        if not new:
            self._debug_log("Comfort learning ran: nothing new to recommend")
        for zone in self.config.zones:
            zone.sync_indigo_device()
        return new

    def print_recommendations(self) -> None:
        items = [r for r in self.recommender.recommendations() if r["status"] == "open"]
        if not items:
            self.logger.info("No open recommendations.")
            return
        self.logger.info(f"💡 {len(items)} open recommendation(s) (beta):")
        for rec in items:
            self.logger.info(f"\t[{rec['id']}] {rec['title']} — {rec['setting_label']} {rec['current_label']} → {rec['proposed_label']}")

    def apply_recommendation(self, rec_id: str):
        """Write the recommended setting into the config file (with backup),
        mark it applied, start its watch, and reload. Returns (ok, message)."""
        from .config_writer import load_config, save_config_with_backup
        rec = next((r for r in self.recommender.recommendations() if r["id"] == rec_id and r["status"] == "open"), None)
        if rec is None:
            return False, "That recommendation is no longer open"
        path = getattr(self.config, "_config_file", None)
        if not path:
            return False, "Config file path unknown"
        try:
            data = load_config(path)
            zone_d = next((z for z in data.get("zones", []) if z.get("name") == rec["zone"]), None)
            if zone_d is None:
                return False, f"Zone '{rec['zone']}' not found in the config file"
            setting = rec["setting"]
            if setting.startswith("comfort_offset."):
                _, season, period = setting.split(".")
                zone_d.setdefault("comfort_offset", {}).setdefault(season, {})[period] = float(rec["proposed"])
            elif setting == "hold_locks_at_night":
                zone_d["hold_locks_at_night"] = str(rec["proposed"])
            elif setting == "lock_duration":
                zone_d["lock_duration"] = int(rec["proposed"])
            elif setting.startswith("night_limits."):
                season = setting.split(".", 1)[1]
                low, high = (int(v) for v in rec["proposed"])
                night = zone_d.setdefault("modifiers", {}).setdefault("nighttime", {}).setdefault(season, {})
                night["clamp_min_pct"], night["clamp_max_pct"] = low, high
            else:
                return False, f"Unknown setting '{setting}'"
            save_config_with_backup(path, data)
        except Exception as e:
            self.logger.exception(f"Applying recommendation {rec_id} failed: {e}")
            return False, f"Could not write the config: {e}"
        self.recommender.mark_applied(rec_id)
        self.recommender.update_snapshot_value(rec["zone"], rec["setting"], rec["proposed"])
        self.logger.info(f"✅ Applied recommendation [{rec_id}]: {rec['setting_label']} "
                         f"{rec['current_label']} → {rec['proposed_label']} for '{rec['zone']}' (watching {14} days)")
        if callable(self.reload_config_callback):
            try:
                self.reload_config_callback()
            except Exception as e:
                self.logger.exception(f"Config reload after applying a recommendation failed: {e}")
        return True, f"Applied: {rec['setting_label']} is now {rec['proposed_label']} for '{rec['zone']}'"

    def dismiss_recommendation(self, rec_id: str):
        rec = self.recommender.mark_dismissed(rec_id)
        if rec is None:
            return False, "That recommendation is no longer open"
        for zone in self.config.zones:
            if zone.name == rec["zone"]:
                zone.sync_indigo_device()
        return True, f"Dismissed: {rec['title']}"

    # ---- Debounced Zone Processing ----

    def _queue_zone_change(self, zone: FanZone, orig_dev, diff: dict) -> None:
        """Queue a sensor change for debounced processing.

        Aggregates rapid device updates within a 1-second window so the zone
        is processed once with a single consolidated log entry instead of once
        per device.
        """
        zone_name = zone.name
        change = self._classify_device_change(zone, orig_dev, diff)

        with self._debounce_lock:
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
        with self._debounce_lock:
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
            released = self._release_lock(zone, reason)
            self.process_zone(zone, trigger="manual unlock", released=released)

    def print_override_summary(self, days: int = 30) -> None:
        """Log the override record summary per zone, season and day/night."""
        rows = self.override_log.summarize(days=days)
        if not rows:
            self.logger.info(f"No manual fan changes recorded in the last {days} days.")
            return
        self.logger.info(f"📒 Manual fan changes, last {days} days:")
        for r in rows:
            delta = r["mean_step_delta"]
            delta_str = f"{delta:+.1f} steps vs plugin" if delta is not None else "n/a"
            self.logger.info(
                f"\t{r['zone']} — {r['season']} {r['period']}: {r['overrides']} override(s) on "
                f"{r['days']} day(s), mean {delta_str}, {r['reoverrides']} within an hour of a "
                f"release, {r['releases']} release(s)"
            )

    def print_locked_zones(self) -> None:
        """Log information about all currently locked zones."""
        locked_zones = [z for z in self.config.zones if z.locked]
        if not locked_zones:
            self.logger.info("No locked zones.")
        else:
            self.logger.info("🔒 Locked Zones:")
            for zone in locked_zones:
                exp = zone.lock_expiration.strftime('%Y-%m-%d %H:%M:%S') if zone.lock_expiration else "N/A"
                rule = "until night ends" if zone.lock_rule == "night_end" else "for a duration"
                self.logger.info(f"🔒 Zone '{zone.name}' locked {rule} — expires {exp} ({zone.lock_reason})")

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
        start_hour, end_hour = zone.get_night_window(season)
        log(f"\t🌙 Night window: {start_hour:02d}:00 → {end_hour:02d}:00 ({'night now' if zone.is_night(season) else 'day now'})")

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
            is_night=zone.is_night(season),
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
            exp = zone.lock_expiration.strftime('%Y-%m-%d %H:%M:%S') if zone.lock_expiration else "N/A"
            log(f"\t🔒 Zone is LOCKED until {exp} ({zone.lock_rule}: {zone.lock_reason})")

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

        # Clean up stale zone devices — collect IDs first to avoid
        # modifying the device list during iteration
        active_indices = {zone.zone_index for zone in self.config.zones}
        stale_devices = []
        for dev in indigo.devices:
            if (
                dev.pluginId == "com.vtmikel.autofan"
                and dev.deviceTypeId == "auto_fan_zone"
            ):
                idx = int(dev.pluginProps.get("zone_index", -1))
                if idx not in active_indices:
                    stale_devices.append((dev.id, dev.name, idx))
        for dev_id, dev_name, idx in stale_devices:
            try:
                indigo.device.delete(dev_id)
                self.logger.info(
                    f"Deleted stale zone device: {dev_name} (index: {idx})"
                )
            except Exception as e:
                self.logger.error(
                    f"Failed to delete stale zone device {dev_name}: {e}"
                )

    def refresh_indigo_device(self, dev_id: int) -> None:
        """Refresh a specific Indigo device's state."""
        for zone in self.config.zones:
            if zone.indigo_dev and zone.indigo_dev.id == dev_id:
                zone.sync_indigo_device()

    def shutdown(self) -> None:
        """Cancel all outstanding timers. A manual change still settling is
        recorded now rather than lost."""
        self.flush_settling_changes()
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        for t in self._offset_timers.values():
            t.cancel()
        self._offset_timers.clear()
        if self._learning_timer:
            self._learning_timer.cancel()
            self._learning_timer = None
        with self._debounce_lock:
            for t in self._debounce_timers.values():
                t.cancel()
        self._debounce_timers.clear()
        self._pending_zone_changes.clear()

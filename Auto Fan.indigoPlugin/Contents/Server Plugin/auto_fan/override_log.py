"""The override record: one JSON line per manual fan change and per lock release.

This is the raw material for comfort learning. Each manual change is written
with the room conditions at that moment (every sensor, the ideal, humidity,
HVAC, presence, outdoor temperature), the step the plugin had set and the
step the person chose. Each lock release is written with the speed the plugin
resumed at, and the next manual change notes how long after that release it
came, so the learner can tell when a lock ended too early.

Files: ``<state dir>/overrides-YYYY-MM.jsonl``, kept for 400 days.
"""
import collections
from datetime import datetime
from typing import Dict, Iterator, List, Optional

from . import clock
from .seasons import get_current_season
from .state_store import StateStore

NAME = "overrides"
KEEP_DAYS = 400
REOVERRIDE_WINDOW_MINUTES = 60


class OverrideLog:
    def __init__(self, store: StateStore) -> None:
        self.store = store
        self._last_release: Dict[str, datetime] = {}
        self.store.prune_jsonl(NAME, KEEP_DAYS)
        # Remember the last release per zone across restarts
        for rec in self.iter_records():
            if rec.get("type") == "release":
                try:
                    self._last_release[rec["zone"]] = datetime.fromisoformat(rec["ts"])
                except (KeyError, TypeError, ValueError):
                    continue

    # ---- writing ----

    def _room_snapshot(self, zone) -> dict:
        season = get_current_season(**zone._config.get_season_kwargs())
        readings = {}
        for _dev_id, name, value in zone.get_sensor_readings():
            readings[name] = value
        temp = zone.get_current_temperature()
        ideal = zone.get_ideal_temperature()
        configured = zone.get_configured_ideal_temperature()
        return {
            "season": season,
            "period": "night" if zone.is_night(season) else "day",
            "temps": readings,
            "temp_avg": round(temp, 2) if temp is not None else None,
            "ideal": round(ideal, 2) if ideal is not None else None,
            "ideal_configured": round(configured, 2) if configured is not None else None,
            "offset_standing": zone.standing_offset_value(),
            "offset_temporary": zone.temporary_offset if zone.temporary_offset_active() else None,
            "humidity": zone.get_humidity(),
            "hvac_state": zone.get_hvac_state(),
            "presence": zone.has_presence_detected(),
            "is_home": zone._config.is_home(),
            "outdoor_temp": zone.get_outdoor_temperature(),
        }

    def record_override(self, zone, fan_name: str, readjust: bool,
                        plugin_step: Optional[int], plugin_pct: Optional[float],
                        human_step: Optional[int], human_pct: Optional[float],
                        implied_ideal: Optional[float] = None,
                        implied_offset: Optional[float] = None) -> dict:
        now = clock.now()
        since_release = None
        last = self._last_release.get(zone.name)
        if last is not None:
            minutes = (now - last).total_seconds() / 60.0
            if 0 <= minutes <= REOVERRIDE_WINDOW_MINUTES:
                since_release = round(minutes, 1)
        rec = {
            "type": "override",
            "ts": now.isoformat(timespec="seconds"),
            "zone": zone.name,
            "fan": fan_name,
            "readjust": bool(readjust),
            "plugin_step": plugin_step,
            "plugin_pct": round(plugin_pct) if plugin_pct is not None else None,
            "human_step": human_step,
            "human_pct": round(human_pct) if human_pct is not None else None,
            "lock_rule": zone.lock_rule,
            "lock_until": zone.lock_expiration.isoformat(timespec="seconds") if zone.lock_expiration else None,
            "minutes_since_release": since_release,
            "implied_ideal": round(implied_ideal, 2) if implied_ideal is not None else None,
            "implied_offset": implied_offset,
        }
        rec.update(self._room_snapshot(zone))
        self.store.append_jsonl(NAME, rec, when=now)
        return rec

    def record_release(self, zone, reason: str, lock_rule: Optional[str],
                       lock_started: Optional[datetime], resumed_pct: Optional[float],
                       resumed_step: Optional[int]) -> dict:
        now = clock.now()
        self._last_release[zone.name] = now
        rec = {
            "type": "release",
            "ts": now.isoformat(timespec="seconds"),
            "zone": zone.name,
            "reason": reason,
            "lock_rule": lock_rule,
            "lock_minutes": round((now - lock_started).total_seconds() / 60.0) if lock_started else None,
            "resumed_pct": round(resumed_pct) if resumed_pct is not None else None,
            "resumed_step": resumed_step,
        }
        rec.update(self._room_snapshot(zone))
        self.store.append_jsonl(NAME, rec, when=now)
        return rec

    # ---- reading ----

    def iter_records(self) -> Iterator[dict]:
        return self.store.iter_jsonl(NAME)

    def records_since(self, since: datetime) -> List[dict]:
        out = []
        for rec in self.iter_records():
            try:
                if datetime.fromisoformat(rec["ts"]) >= since:
                    out.append(rec)
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def summarize(self, days: int = 30, now: Optional[datetime] = None) -> List[dict]:
        """Per (zone, season, period): overrides, distinct days, mean step change,
        re-overrides within an hour of a release, releases."""
        now = now or clock.now()
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = since.fromordinal(since.toordinal() - days)
        buckets: Dict[tuple, dict] = collections.OrderedDict()
        for rec in self.records_since(since):
            key = (rec.get("zone"), rec.get("season"), rec.get("period"))
            b = buckets.setdefault(key, {
                "zone": key[0], "season": key[1], "period": key[2],
                "overrides": 0, "days": set(), "step_deltas": [], "reoverrides": 0, "releases": 0,
            })
            if rec.get("type") == "override":
                b["overrides"] += 1
                b["days"].add(rec["ts"][:10])
                if rec.get("human_step") is not None and rec.get("plugin_step") is not None:
                    b["step_deltas"].append(rec["human_step"] - rec["plugin_step"])
                if rec.get("minutes_since_release") is not None:
                    b["reoverrides"] += 1
            elif rec.get("type") == "release":
                b["releases"] += 1
        out = []
        for b in buckets.values():
            deltas = b.pop("step_deltas")
            b["days"] = len(b["days"])
            b["mean_step_delta"] = round(sum(deltas) / len(deltas), 2) if deltas else None
            out.append(b)
        return out

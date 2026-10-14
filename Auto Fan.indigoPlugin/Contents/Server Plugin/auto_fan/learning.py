"""Comfort learning: turn the override record into recommendations.

Runs once a day (at the Daily Reset Hour) and on demand. For every zone,
season and half of the day it looks at the manual changes people made and
asks what standing comfort offset would have made them unnecessary. It also
looks at how often a lock ended and was re-overridden within the hour, which
means the lock rule is too short.

Everything here is a recommendation: nothing changes until someone applies it
from the zone's page. Guardrails:

- evidence: at least 3 overrides on 3 distinct days per bucket (nights for
  the night half: a change at 03:00 belongs to the night that began the
  evening before), each day or night weighing the same however many changes
  it holds;
- decay: evidence loses half its weight every 21 days;
- bounds: offsets stay within ±3 °F in 0.5 °F steps;
- pace: nothing within 7 days of a change to the same setting, and one open
  recommendation per setting per zone;
- observe period: nothing until the record is 7 days old;
- watch: an applied change is compared 14 days later with the 14 days before
  it; if overrides did not fall, a revert is recommended.
"""
import math
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from . import clock
from .comfort import OFFSET_LIMIT, round_offset
from .seasons import SEASONS

MIN_OVERRIDES = 3
MIN_DAYS = 3
HALF_LIFE_DAYS = 21.0
PACE_DAYS = 7
WATCH_DAYS = 14
OBSERVE_DAYS = 7
EVIDENCE_WINDOW_DAYS = 60
REOVERRIDE_RATE = 0.30
MIN_LOCKS_FOR_RULE = 5

RECS = "recommendations"
FIELD_CHANGES = "field_changes"
SNAPSHOT = "learnable_snapshot"


def _parse_ts(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def occasion(record: dict, ts: datetime, zone=None) -> date:
    """The day a change belongs to: its date, or for a change in the night
    half, the date the night began (with the zone's night window for the
    record's season; a 20:00–08:00 night puts 03:00 on the evening before)."""
    if record.get("period") != "night" or zone is None:
        return ts.date()
    start_hour, end_hour = zone.get_night_window(record.get("season"))
    if start_hour > end_hour and ts.hour < end_hour:
        return ts.date() - timedelta(days=1)
    return ts.date()


def weighted_median(pairs: List[tuple]) -> Optional[float]:
    """Median of (value, weight) pairs."""
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2.0:
            return v
    return pairs[-1][0]


class Recommender:
    def __init__(self, config, override_log, store) -> None:
        self.config = config
        self.override_log = override_log
        self.store = store

    # ---- state ----

    def recommendations(self) -> List[dict]:
        data = self.store.load(RECS, {"items": []}) or {"items": []}
        return list(data.get("items", []))

    def _save_recommendations(self, items: List[dict]) -> None:
        self.store.save(RECS, {"items": items})

    def open_for_zone(self, zone_name: str) -> List[dict]:
        return [r for r in self.recommendations() if r["zone"] == zone_name and r["status"] == "open"]

    def _field_changes(self) -> Dict[str, str]:
        return self.store.load(FIELD_CHANGES, {}) or {}

    def record_field_change(self, zone_name: str, setting: str, when: Optional[datetime] = None) -> None:
        changes = self._field_changes()
        changes[f"{zone_name}|{setting}"] = (when or clock.now()).isoformat(timespec="seconds")
        self.store.save(FIELD_CHANGES, changes)

    def _changed_recently(self, zone_name: str, setting: str, now: datetime) -> bool:
        ts = _parse_ts(self._field_changes().get(f"{zone_name}|{setting}", ""))
        return ts is not None and (now - ts) < timedelta(days=PACE_DAYS)

    # ---- settings the learner may touch ----

    @staticmethod
    def learnable_values(zone) -> Dict[str, object]:
        """Current values of every setting the learner may recommend, keyed by setting name."""
        out = {}
        for season in SEASONS:
            for period in ("day", "night"):
                out[f"comfort_offset.{season}.{period}"] = zone.standing_offset_value(season, period == "night")
        out["hold_locks_at_night"] = zone.hold_locks_at_night
        out["lock_duration"] = zone.lock_duration
        return out

    def note_hand_edits(self, zones) -> List[str]:
        """Compare the learnable settings with the last snapshot; a value that
        changed outside apply_recommendation is a hand edit: it restarts the
        pace timer and dismisses any open recommendation for that setting."""
        previous = self.store.load(SNAPSHOT, {}) or {}
        current = {z.name: self.learnable_values(z) for z in zones}
        edited = []
        now = clock.now()
        for zone_name, values in current.items():
            before = previous.get(zone_name)
            if before is None:
                continue
            for setting, value in values.items():
                if setting in before and before[setting] != value:
                    edited.append(f"{zone_name}|{setting}")
                    self.record_field_change(zone_name, setting, now)
        if edited:
            items = self.recommendations()
            for r in items:
                if r["status"] == "open" and f"{r['zone']}|{r['setting']}" in edited:
                    r["status"] = "dismissed"
                    r["dismissed_reason"] = "setting changed by hand"
            self._save_recommendations(items)
        self.store.save(SNAPSHOT, current)
        return edited

    def update_snapshot_value(self, zone_name: str, setting: str, value) -> None:
        """After applying a recommendation, teach the snapshot the new value so
        the next reload does not read it as a hand edit."""
        snap = self.store.load(SNAPSHOT, {}) or {}
        snap.setdefault(zone_name, {})[setting] = value
        self.store.save(SNAPSHOT, snap)

    # ---- the daily run ----

    def learning_enabled_for(self, zone) -> bool:
        plugin_mode = str(getattr(self.config, "learning_mode", "suggest") or "suggest")
        zone_mode = str(getattr(zone, "learning_mode", "inherit") or "inherit")
        if zone_mode == "off":
            return False
        if zone_mode == "suggest":
            return True
        return plugin_mode != "off"

    def run(self, now: Optional[datetime] = None) -> List[dict]:
        """Create new recommendations and settle finished watches. Returns the new ones."""
        now = now or clock.now()
        records = list(self.override_log.iter_records())
        if not records:
            return []
        first = min((_parse_ts(r.get("ts", "")) for r in records if _parse_ts(r.get("ts", ""))), default=None)
        if first is None or (now - first) < timedelta(days=OBSERVE_DAYS):
            return []  # observe period: evidence only
        items = self.recommendations()
        new = self._settle_watches(items, records, now)  # reverts are new recommendations too
        for zone in self.config.zones:
            if not self.learning_enabled_for(zone):
                continue
            new.extend(self._offset_recommendations(zone, records, items, now))
            rec = self._lock_rule_recommendation(zone, records, items, now)
            if rec:
                new.append(rec)
        # reverts from _settle_watches are already in items; add the rest
        items.extend(r for r in new if r not in items)
        self._save_recommendations(items)
        return new

    # ---- comfort offset ----

    def _offset_recommendations(self, zone, records: List[dict], items: List[dict], now: datetime) -> List[dict]:
        since = now - timedelta(days=EVIDENCE_WINDOW_DAYS)
        buckets: Dict[tuple, list] = {}
        for r in records:
            if r.get("type") != "override" or r.get("zone") != zone.name:
                continue
            ts = _parse_ts(r.get("ts", ""))
            if ts is None or ts < since or r.get("implied_offset") is None:
                continue
            key = (r.get("season"), r.get("period"))
            if key[0] not in SEASONS or key[1] not in ("day", "night"):
                continue
            wanted = float(r.get("offset_standing") or 0.0) + float(r["implied_offset"])
            weight = math.pow(0.5, (now - ts).total_seconds() / 86400.0 / HALF_LIFE_DAYS)
            buckets.setdefault(key, []).append((occasion(r, ts, zone), wanted, weight))
        out = []
        for (season, period), evidence in buckets.items():
            setting = f"comfort_offset.{season}.{period}"
            per_day: Dict[date, int] = {}
            for day, _, _ in evidence:
                per_day[day] = per_day.get(day, 0) + 1
            days = set(per_day)
            if len(evidence) < MIN_OVERRIDES or len(days) < MIN_DAYS:
                continue
            if self._changed_recently(zone.name, setting, now):
                continue
            if any(r["zone"] == zone.name and r["setting"] == setting and r["status"] == "open" for r in items):
                continue
            # each day (night) counts once, however many changes it holds
            median = weighted_median([(w_, wt / per_day[day]) for day, w_, wt in evidence])
            if median is None:
                continue
            proposed = round_offset(median)
            current = float(zone.standing_offset_value(season, period == "night"))
            if proposed == current:
                continue
            raised = sum(1 for _, wanted, _ in evidence if wanted < current)  # wanted a cooler ideal
            direction = "cooler" if proposed < current else "warmer"
            text = (
                f"In the last {EVIDENCE_WINDOW_DAYS} days the {zone.name} fan was changed by hand "
                f"{len(evidence)} time(s) on {len(days)} different {'night' if period == 'night' else 'day'}(s) "
                f"during {season} {period}s; "
                f"{raised} of them asked for a {direction} room than the current setting. "
                f"The changes point to an ideal about {abs(proposed - current):.1f}°F {direction}."
            )
            out.append({
                "id": uuid.uuid4().hex[:12],
                "zone": zone.name,
                "setting": setting,
                "setting_label": f"{season.capitalize()} {period} comfort offset",
                "title": f"{zone.name}: people want it {direction} on {season} {period}s",
                "text": text,
                "current": current,
                "proposed": proposed,
                "current_label": f"{current:+.1f}°F",
                "proposed_label": f"{proposed:+.1f}°F",
                "evidence": {"overrides": len(evidence), "days": len(days), "window_days": EVIDENCE_WINDOW_DAYS,
                             "weighted_median_wanted": round(median, 2)},
                "status": "open",
                "created": now.isoformat(timespec="seconds"),
                "bucket": {"season": season, "period": period},
            })
        return out

    # ---- lock rule ----

    def _lock_rule_recommendation(self, zone, records: List[dict], items: List[dict], now: datetime) -> Optional[dict]:
        since = now - timedelta(days=30)
        releases = [r for r in records if r.get("type") == "release" and r.get("zone") == zone.name
                    and (_parse_ts(r.get("ts", "")) or since) >= since
                    and r.get("reason") in ("expired", "night ended")]
        reoverrides = [r for r in records if r.get("type") == "override" and r.get("zone") == zone.name
                       and (_parse_ts(r.get("ts", "")) or since) >= since
                       and r.get("minutes_since_release") is not None]
        if len(releases) < MIN_LOCKS_FOR_RULE:
            return None
        rate = len(reoverrides) / float(len(releases))
        if rate <= REOVERRIDE_RATE:
            return None
        night_reoverrides = sum(1 for r in reoverrides if r.get("period") == "night")
        if not zone.holds_locks_at_night() and night_reoverrides * 2 >= len(reoverrides):
            setting, current, proposed = "hold_locks_at_night", zone.hold_locks_at_night, "on"
            current_label, proposed_label = str(current), "on"
            title = f"{zone.name}: hold locks at nighttime"
            text = (f"In the last 30 days {len(reoverrides)} of {len(releases)} lock releases were followed by another "
                    f"manual change within the hour, {night_reoverrides} of them at night. Holding nighttime locks until the "
                    f"night window ends would stop the fan being changed back after the person adjusted it.")
            label = "Hold locks at nighttime"
        else:
            setting = "lock_duration"
            current = zone.lock_duration if zone.lock_duration >= 0 else zone.get_effective_lock_duration()
            proposed = int(math.ceil(current * 1.5 / 5.0) * 5)
            current_label, proposed_label = f"{current} min", f"{proposed} min"
            title = f"{zone.name}: locks end too soon"
            text = (f"In the last 30 days {len(reoverrides)} of {len(releases)} lock releases were followed by another "
                    f"manual change within the hour. A lock duration half again as long would cover most of them.")
            label = "Lock duration"
        if self._changed_recently(zone.name, setting, now):
            return None
        if any(r["zone"] == zone.name and r["setting"] == setting and r["status"] == "open" for r in items):
            return None
        return {
            "id": uuid.uuid4().hex[:12], "zone": zone.name, "setting": setting, "setting_label": label,
            "title": title, "text": text, "current": current, "proposed": proposed,
            "current_label": current_label, "proposed_label": proposed_label,
            "evidence": {"releases": len(releases), "reoverrides": len(reoverrides), "rate": round(rate, 2), "window_days": 30},
            "status": "open", "created": now.isoformat(timespec="seconds"), "bucket": None,
        }

    # ---- apply / dismiss / watch ----

    def _bucket_count(self, records: List[dict], rec: dict, start: datetime, end: datetime) -> int:
        """Days (nights, for a night bucket) between start and end on which
        the zone's fan was changed by hand, counted like the evidence."""
        zone = next((z for z in self.config.zones if z.name == rec["zone"]), None)
        days = set()
        for r in records:
            if r.get("type") != "override" or r.get("zone") != rec["zone"]:
                continue
            ts = _parse_ts(r.get("ts", ""))
            if ts is None or not (start <= ts < end):
                continue
            b = rec.get("bucket")
            if b and (r.get("season") != b["season"] or r.get("period") != b["period"]):
                continue
            days.add(occasion(r, ts, zone))
        return len(days)

    def mark_applied(self, rec_id: str, now: Optional[datetime] = None) -> Optional[dict]:
        now = now or clock.now()
        items = self.recommendations()
        records = list(self.override_log.iter_records())
        target = None
        for r in items:
            if r["id"] == rec_id and r["status"] == "open":
                r["status"] = "applied"
                r["applied_at"] = now.isoformat(timespec="seconds")
                r["watch_until"] = (now + timedelta(days=WATCH_DAYS)).isoformat(timespec="seconds")
                r["baseline_overrides"] = self._bucket_count(records, r, now - timedelta(days=WATCH_DAYS), now)
                target = r
        if target:
            self.record_field_change(target["zone"], target["setting"], now)
            self._save_recommendations(items)
        return target

    def mark_dismissed(self, rec_id: str) -> Optional[dict]:
        items = self.recommendations()
        target = None
        for r in items:
            if r["id"] == rec_id and r["status"] == "open":
                r["status"] = "dismissed"
                r["dismissed_reason"] = "dismissed by a person"
                target = r
        if target:
            self._save_recommendations(items)
        return target

    def _settle_watches(self, items: List[dict], records: List[dict], now: datetime) -> List[dict]:
        """Finish watches that are due; append (and return) revert recommendations."""
        reverts = []
        for r in list(items):
            if r.get("status") != "applied":
                continue
            until = _parse_ts(r.get("watch_until", ""))
            applied = _parse_ts(r.get("applied_at", ""))
            if until is None or applied is None or now < until:
                continue
            after = self._bucket_count(records, r, applied, until)
            before = int(r.get("baseline_overrides") or 0)
            r["watch_result"] = {"before": before, "after": after}
            if after < before:
                r["status"] = "confirmed"
                continue
            r["status"] = "watch_failed"
            revert_label = r.get("current_label", str(r.get("current")))
            unit = "night" if (r.get("bucket") or {}).get("period") == "night" else "day"
            revert = {
                "id": uuid.uuid4().hex[:12], "zone": r["zone"], "setting": r["setting"],
                "setting_label": r.get("setting_label", r["setting"]),
                "title": f"{r['zone']}: the last change did not help — revert it",
                "text": (f"After changing {r.get('setting_label', r['setting'])} to {r.get('proposed_label')} the fan was "
                         f"changed by hand on {after} {unit}(s) in {WATCH_DAYS} days, against {before} in the {WATCH_DAYS} days "
                         f"before. Recommended: go back to {revert_label}."),
                "current": r["proposed"], "proposed": r["current"],
                "current_label": r.get("proposed_label"), "proposed_label": revert_label,
                "evidence": {"before": before, "after": after, "watch_days": WATCH_DAYS},
                "status": "open", "created": now.isoformat(timespec="seconds"),
                "bucket": r.get("bucket"), "revert_of": r["id"],
            }
            items.append(revert)
            reverts.append(revert)
        return reverts

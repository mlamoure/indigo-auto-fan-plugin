"""Comfort learning: turn the override record into recommendations.

Runs once a day (at the Daily Reset Hour) and on demand. For every zone,
season and half of the day it looks at the manual changes people made and
asks what standing comfort offset would have made them unnecessary. It also
looks at how often a lock ended and was re-overridden within the hour, which
means the lock rule is too short, and at nights when people turned the fan up
on some nights and down on others, which no offset can fit: those get a
recommendation for the night speed limits instead.

Everything here is a recommendation: nothing changes until someone applies it
from the zone's page. Guardrails:

- evidence: at least 3 decisions on 3 distinct days per bucket (nights for
  the night half: a change at 03:00 belongs to the night that began the
  evening before); changes less than 15 minutes apart are one decision,
  counted where it ended; each day or night weighs the same however many
  decisions it holds; a change at either end of the speed curve counts only when it
  points away from that end (comfort.offset_is_informative);
- decay: evidence loses half its weight every 21 days;
- bounds: offsets stay within ±3 °F in 0.5 °F steps;
- pace: nothing within 7 days of a change to the same setting, and one open
  recommendation per setting per zone; each run re-reads the evidence and
  replaces an open recommendation it no longer agrees with, or withdraws it
  when the evidence no longer supports any change (an open revert stays until
  a person acts on it);
- observe period: nothing until the record is 7 days old;
- watch: an applied change is compared 14 days later with the 14 days before
  it, as the share of counted days (nights) with a manual change; a day counts
  when the record had started, it falls in the recommendation's season and
  the house was home (from the day presence tracking began). If that share
  did not fall, a revert is recommended.
"""
import math
import uuid
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional

from . import clock
from .comfort import offset_is_informative, round_offset
from .seasons import SEASONS, get_current_season

MIN_OVERRIDES = 3
MIN_DAYS = 3
HALF_LIFE_DAYS = 21.0
PACE_DAYS = 7
WATCH_DAYS = 14
OBSERVE_DAYS = 7
EVIDENCE_WINDOW_DAYS = 60
REOVERRIDE_RATE = 0.30
MIN_LOCKS_FOR_RULE = 5
# Night speed limits: nights with a manual change, of which at least this
# share went up and this share went down (and at least 2 each way)
# Manual changes less than this far apart are one decision: the person was
# still finding the speed they wanted, so only where they ended up counts
SESSION_MINUTES = 15
SWING_MIN_NIGHTS = 5
SWING_SHARE = 0.20
LIMIT_STEP = 10          # the web editor offers night limits in 10% steps
HOME_KEEP_DAYS = 120

RECS = "recommendations"
FIELD_CHANGES = "field_changes"
SNAPSHOT = "learnable_snapshot"
HOME_DAYS = "home_days"

LOCK_SETTINGS = ("lock_duration", "hold_locks_at_night")


def _parse_ts(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def section_for(setting: str) -> str:
    """The zone-page section that owns a setting: comfort, lock or nighttime."""
    if setting in LOCK_SETTINGS:
        return "lock"
    if setting.startswith("night_limits."):
        return "nighttime"
    return "comfort"


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


def settled_changes(records: List[dict], zone_name: str) -> List[dict]:
    """A zone's override records with each adjustment session reduced to its
    last change. A session is a run of changes each less than SESSION_MINUTES
    after the one before (set 43%, then 29% ten minutes later: one decision,
    29%). Changes further apart, such as one at bedtime and one at 04:00,
    stay separate decisions."""
    ordered = []
    for r in records:
        if r.get("type") != "override" or r.get("zone") != zone_name:
            continue
        ts = _parse_ts(r.get("ts", ""))
        if ts is not None:
            ordered.append((ts, r))
    ordered.sort(key=lambda c: c[0])
    out = []
    for i, (ts, r) in enumerate(ordered):
        nxt = ordered[i + 1][0] if i + 1 < len(ordered) else None
        if nxt is None or (nxt - ts) >= timedelta(minutes=SESSION_MINUTES):
            out.append(r)
    return out


def _percentile(values: List[float], share: float) -> float:
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    rank = max(1, math.ceil(share * len(ordered)))
    return ordered[rank - 1]


def _round_limit(pct: float) -> int:
    return int(max(0, min(100, round(pct / LIMIT_STEP) * LIMIT_STEP)))


def night_limits(zone, season: str) -> List[int]:
    """[min, max] night speed limits for a season; [0, 100] means none."""
    mod = ((getattr(zone, "modifiers", None) or {}).get("nighttime") or {}).get(season) or {}
    lo, hi = mod.get("clamp_min_pct"), mod.get("clamp_max_pct")
    return [0 if lo is None else int(lo), 100 if hi is None else int(hi)]


def limits_label(limits) -> str:
    lo, hi = int(limits[0]), int(limits[1])
    return f"min {'none' if lo <= 0 else f'{lo}%'}, max {'none' if hi >= 100 else f'{hi}%'}"


class Recommender:
    def __init__(self, config, override_log, store) -> None:
        self.config = config
        self.override_log = override_log
        self.store = store
        self._home_noted: Optional[date] = None   # dates already handled, so a
        self._away_noted: Optional[date] = None   # zone evaluation reads nothing

    # ---- state ----

    def recommendations(self) -> List[dict]:
        data = self.store.load(RECS, {"items": []}) or {"items": []}
        return list(data.get("items", []))

    def _save_recommendations(self, items: List[dict]) -> None:
        self.store.save(RECS, {"items": items})

    def open_for_zone(self, zone_name: str) -> List[dict]:
        return [dict(r, section=section_for(r["setting"])) for r in self.recommendations()
                if r["zone"] == zone_name and r["status"] == "open"]

    def _field_changes(self) -> Dict[str, str]:
        return self.store.load(FIELD_CHANGES, {}) or {}

    def record_field_change(self, zone_name: str, setting: str, when: Optional[datetime] = None) -> None:
        changes = self._field_changes()
        changes[f"{zone_name}|{setting}"] = (when or clock.now()).isoformat(timespec="seconds")
        self.store.save(FIELD_CHANGES, changes)

    def _changed_recently(self, zone_name: str, setting: str, now: datetime) -> bool:
        ts = _parse_ts(self._field_changes().get(f"{zone_name}|{setting}", ""))
        return ts is not None and (now - ts) < timedelta(days=PACE_DAYS)

    # ---- presence: which days count for the watch ----

    def note_home(self, now: datetime, is_home: bool) -> None:
        """Remember the dates the house was home at some point. Days from the
        first call on count for the watch only when noted; earlier days are
        assumed home. Writes at most once a day."""
        today = now.date()
        if self._home_noted == today or (not is_home and self._away_noted == today):
            return
        data = self.store.load(HOME_DAYS, {}) or {}
        dates = set(data.get("dates") or [])
        since = data.get("since") or today.isoformat()
        changed = "since" not in data
        if is_home and today.isoformat() not in dates:
            dates.add(today.isoformat())
            cutoff = (today - timedelta(days=HOME_KEEP_DAYS)).isoformat()
            dates = {d for d in dates if d >= cutoff}
            changed = True
        if changed:
            self.store.save(HOME_DAYS, {"since": since, "dates": sorted(dates)})
        if is_home:
            self._home_noted = today
        else:
            self._away_noted = today

    def _counted_days(self, records: List[dict], bucket: Optional[dict], start: datetime, end: datetime) -> int:
        """Days (nights) between start and end that can show a manual change:
        on or after the first record, in the bucket's season, and home."""
        first = min((ts.date() for ts in (_parse_ts(r.get("ts", "")) for r in records) if ts), default=None)
        if first is None:
            return 0
        home = self.store.load(HOME_DAYS, {}) or {}
        home_since = home.get("since")
        home_dates = set(home.get("dates") or [])
        season = (bucket or {}).get("season")
        kwargs = self.config.get_season_kwargs() if hasattr(self.config, "get_season_kwargs") else {}
        count = 0
        day = start.date()
        while day < end.date():
            iso = day.isoformat()
            if (day >= first
                    and (home_since is None or iso < home_since or iso in home_dates)
                    and (season is None or get_current_season(now=datetime.combine(day, time(12)), **kwargs) == season)):
                count += 1
            day += timedelta(days=1)
        return count

    # ---- settings the learner may touch ----

    @staticmethod
    def learnable_values(zone) -> Dict[str, object]:
        """Current values of every setting the learner may recommend, keyed by setting name."""
        out = {}
        for season in SEASONS:
            for period in ("day", "night"):
                out[f"comfort_offset.{season}.{period}"] = zone.standing_offset_value(season, period == "night")
            out[f"night_limits.{season}"] = night_limits(zone, season)
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
        """Create new recommendations, refresh or withdraw open ones, and
        settle finished watches. Returns the new ones."""
        now = now or clock.now()
        records = list(self.override_log.iter_records())
        if not records:
            return []
        first = min((_parse_ts(r.get("ts", "")) for r in records if _parse_ts(r.get("ts", ""))), default=None)
        if first is None or (now - first) < timedelta(days=OBSERVE_DAYS):
            return []  # observe period: evidence only
        items = self.recommendations()
        self._dedupe_open(items, now)
        new = self._settle_watches(items, records, now)  # reverts are new recommendations too
        for zone in self.config.zones:
            if not self.learning_enabled_for(zone):
                continue
            new.extend(self._offset_and_limit_recommendations(zone, records, items, now))
            new.extend(self._lock_rule_recommendations(zone, records, items, now))
        # reverts from _settle_watches are already in items; add the rest
        items.extend(r for r in new if r not in items)
        self._save_recommendations(items)
        return new

    # ---- keeping one current recommendation per setting ----

    @staticmethod
    def _close(rec: dict, status: str, reason: str, now: datetime, by: Optional[str] = None) -> None:
        rec["status"] = status
        rec[f"{status}_reason"] = reason
        rec["closed"] = now.isoformat(timespec="seconds")
        if by:
            rec["superseded_by"] = by

    def _dedupe_open(self, items: List[dict], now: datetime) -> None:
        """At most one open recommendation per zone and setting: an open
        revert wins, otherwise the newest."""
        groups: Dict[tuple, List[dict]] = {}
        for r in items:
            if r.get("status") == "open":
                groups.setdefault((r["zone"], r["setting"]), []).append(r)
        for recs in groups.values():
            if len(recs) < 2:
                continue
            keep = max(recs, key=lambda r: (bool(r.get("revert_of")), r.get("created", "")))
            for r in recs:
                if r is not keep:
                    self._close(r, "superseded", "a newer recommendation for this setting replaced it", now, keep["id"])

    def _reconcile(self, zone, setting: str, candidate: Optional[dict], items: List[dict],
                   now: datetime, withdraw_reason: str) -> Optional[dict]:
        """Decide what today's evidence does to the open recommendation for a
        setting. Returns the candidate when it should be added as new."""
        open_recs = [r for r in items if r["zone"] == zone.name and r["setting"] == setting and r["status"] == "open"]
        if any(r.get("revert_of") for r in open_recs):
            return None  # a revert waits for a person
        if candidate is None:
            for r in open_recs:
                self._close(r, "withdrawn", withdraw_reason, now)
            return None
        if self._changed_recently(zone.name, setting, now):
            return None
        same = [r for r in open_recs if r.get("proposed") == candidate["proposed"]]
        if same:
            keep = same[-1]
            for key in ("title", "text", "evidence", "current", "current_label"):
                keep[key] = candidate[key]
            keep["refreshed"] = now.isoformat(timespec="seconds")
            for r in open_recs:
                if r is not keep:
                    self._close(r, "superseded", "a newer recommendation for this setting replaced it", now, keep["id"])
            return None
        for r in open_recs:
            self._close(r, "superseded", "newer evidence points to a different value", now, candidate["id"])
        return candidate

    # ---- comfort offset and night speed limits ----

    def _night_swing(self, zone, season: str, records: List[dict], since: datetime) -> Optional[dict]:
        """Nights when people turned automation's speed up on some and down on
        others: an offset moves the whole curve one way and cannot fit them.
        Returns the band where people left the fan, or None."""
        nights: Dict[date, List[tuple]] = {}
        for r in records:
            if (r.get("type") != "override" or r.get("zone") != zone.name
                    or r.get("season") != season or r.get("period") != "night"):
                continue
            ts = _parse_ts(r.get("ts", ""))
            if ts is None or ts < since or r.get("human_pct") is None or r.get("human_step") is None:
                continue
            nights.setdefault(occasion(r, ts, zone), []).append((ts, r))
        raised = lowered = 0
        finals = []
        for changes in nights.values():
            changes.sort(key=lambda c: c[0])
            # automation's step: from the night's first change that records it
            # (a re-adjustment can lack it); where people left it: the last change
            auto_step = next((c[1]["plugin_step"] for c in changes if c[1].get("plugin_step") is not None), None)
            if auto_step is None:
                continue
            last = changes[-1][1]
            finals.append(float(last["human_pct"]))
            if last["human_step"] > auto_step:
                raised += 1
            elif last["human_step"] < auto_step:
                lowered += 1
        n = len(finals)
        if n < SWING_MIN_NIGHTS or min(raised, lowered) < max(2, math.ceil(SWING_SHARE * n)):
            return None
        # the middle 80% of nights: a ceiling set too low only brings the
        # warm nights back as manual changes
        lo, hi = _percentile(finals, 0.1), _percentile(finals, 0.9)
        lo_limit, hi_limit = _round_limit(lo), _round_limit(hi)
        if hi_limit <= lo_limit:
            hi_limit = min(100, lo_limit + LIMIT_STEP)
        return {"nights": n, "raised": raised, "lowered": lowered,
                "low_pct": round(lo), "high_pct": round(hi), "limits": [lo_limit, hi_limit]}

    def _limits_candidate(self, zone, season: str, swing: Optional[dict], now: datetime) -> Optional[dict]:
        if swing is None:
            return None
        current = night_limits(zone, season)
        proposed = swing["limits"]
        if proposed == current:
            return None
        text = (
            f"In the last {EVIDENCE_WINDOW_DAYS} days the {zone.name} fan was changed by hand on {swing['nights']} "
            f"{season} night(s): turned up on {swing['raised']} and down on {swing['lowered']}. A comfort offset moves "
            f"the whole curve one way, so it cannot fit both. On most of those nights people left the fan between "
            f"{swing['low_pct']}% and {swing['high_pct']}%; night speed limits of {limits_label(proposed)} keep "
            f"automation in that range."
        )
        return {
            "id": uuid.uuid4().hex[:12],
            "zone": zone.name,
            "setting": f"night_limits.{season}",
            "setting_label": f"{season.capitalize()} night speed limits",
            "title": f"{zone.name}: set night speed limits for {season}",
            "text": text,
            "current": current,
            "proposed": proposed,
            "current_label": limits_label(current),
            "proposed_label": limits_label(proposed),
            "evidence": {k: swing[k] for k in ("nights", "raised", "lowered", "low_pct", "high_pct")}
                        | {"window_days": EVIDENCE_WINDOW_DAYS},
            "status": "open",
            "created": now.isoformat(timespec="seconds"),
            "bucket": {"season": season, "period": "night"},
        }

    def _offset_candidates(self, zone, records: List[dict], now: datetime) -> Dict[str, dict]:
        since = now - timedelta(days=EVIDENCE_WINDOW_DAYS)
        buckets: Dict[tuple, list] = {}
        for r in settled_changes(records, zone.name):
            ts = _parse_ts(r.get("ts", ""))
            if ts is None or ts < since or r.get("implied_offset") is None:
                continue
            key = (r.get("season"), r.get("period"))
            if key[0] not in SEASONS or key[1] not in ("day", "night"):
                continue
            curve = (getattr(zone, "seasonal_curves", None) or {}).get(key[0]) or {}
            if not offset_is_informative(r.get("human_pct"), r.get("implied_offset"), curve.get("points") or []):
                continue  # e.g. turned off below the curve's lowest speed in a cold room
            wanted = float(r.get("offset_standing") or 0.0) + float(r["implied_offset"])
            weight = math.pow(0.5, (now - ts).total_seconds() / 86400.0 / HALF_LIFE_DAYS)
            buckets.setdefault(key, []).append((occasion(r, ts, zone), wanted, weight))
        out = {}
        for (season, period), evidence in buckets.items():
            setting = f"comfort_offset.{season}.{period}"
            per_day: Dict[date, int] = {}
            for day, _, _ in evidence:
                per_day[day] = per_day.get(day, 0) + 1
            days = set(per_day)
            if len(evidence) < MIN_OVERRIDES or len(days) < MIN_DAYS:
                continue
            # each day (night) counts once, however many changes it holds
            median = weighted_median([(w_, wt / per_day[day]) for day, w_, wt in evidence])
            if median is None:
                continue
            proposed = round_offset(median)
            current = float(zone.standing_offset_value(season, period == "night"))
            if proposed == current:
                continue
            direction = "cooler" if proposed < current else "warmer"
            if direction == "cooler":
                agreeing = sum(1 for _, wanted, _ in evidence if wanted < current)
            else:
                agreeing = sum(1 for _, wanted, _ in evidence if wanted > current)
            unit = "night" if period == "night" else "day"
            text = (
                f"In the last {EVIDENCE_WINDOW_DAYS} days the {zone.name} fan was changed by hand "
                f"{len(evidence)} time(s) on {len(days)} different {unit}(s) during {season} {period}s; "
                f"{agreeing} of them asked for a {direction} room than the current setting. "
                f"The changes point to an ideal about {abs(proposed - current):.1f}°F {direction}."
            )
            out[setting] = {
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
            }
        return out

    def _offset_and_limit_recommendations(self, zone, records: List[dict], items: List[dict],
                                          now: datetime) -> List[dict]:
        since = now - timedelta(days=EVIDENCE_WINDOW_DAYS)
        offsets = self._offset_candidates(zone, records, now)
        out = []
        for season in SEASONS:
            swing = self._night_swing(zone, season, records, since)
            rec = self._reconcile(zone, f"night_limits.{season}", self._limits_candidate(zone, season, swing, now),
                                  items, now, "the night limits already fit, or the changes no longer swing both ways")
            if rec:
                out.append(rec)
            for period in ("day", "night"):
                setting = f"comfort_offset.{season}.{period}"
                if period == "night" and swing is not None:
                    candidate, reason = None, "the night changes swing both ways; night speed limits fit them better"
                else:
                    candidate, reason = offsets.get(setting), "the evidence no longer supports a change"
                rec = self._reconcile(zone, setting, candidate, items, now, reason)
                if rec:
                    out.append(rec)
        return out

    # ---- lock rule ----

    def _lock_rule_candidate(self, zone, records: List[dict], now: datetime) -> Optional[dict]:
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
        return {
            "id": uuid.uuid4().hex[:12], "zone": zone.name, "setting": setting, "setting_label": label,
            "title": title, "text": text, "current": current, "proposed": proposed,
            "current_label": current_label, "proposed_label": proposed_label,
            "evidence": {"releases": len(releases), "reoverrides": len(reoverrides), "rate": round(rate, 2), "window_days": 30},
            "status": "open", "created": now.isoformat(timespec="seconds"), "bucket": None,
        }

    def _lock_rule_recommendations(self, zone, records: List[dict], items: List[dict], now: datetime) -> List[dict]:
        candidate = self._lock_rule_candidate(zone, records, now)
        out = []
        for setting in LOCK_SETTINGS:
            mine = candidate if candidate and candidate["setting"] == setting else None
            rec = self._reconcile(zone, setting, mine, items, now,
                                  "locks are no longer being re-overridden often enough")
            if rec:
                out.append(rec)
        return out

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
        """Finish watches that are due; append (and return) revert recommendations.

        Compares the share of counted days (nights) with a manual change in
        the 14 days before and after the change, so days before the record
        began, out of season or away do not count against either side."""
        reverts = []
        for r in list(items):
            if r.get("status") != "applied":
                continue
            until = _parse_ts(r.get("watch_until", ""))
            applied = _parse_ts(r.get("applied_at", ""))
            if until is None or applied is None or now < until:
                continue
            bucket = r.get("bucket")
            after = self._bucket_count(records, r, applied, until)
            before = int(r.get("baseline_overrides") or 0)
            before_days = self._counted_days(records, bucket, applied - timedelta(days=WATCH_DAYS), applied)
            after_days = self._counted_days(records, bucket, applied, until)
            r["watch_result"] = {"before": before, "after": after,
                                 "before_days": before_days, "after_days": after_days}
            if before_days == 0 or after_days == 0:
                r["status"] = "watch_inconclusive"
                continue
            if after / after_days < before / before_days:
                r["status"] = "confirmed"
                continue
            if before == 0 and after == 0:
                r["status"] = "watch_inconclusive"
                continue
            r["status"] = "watch_failed"
            revert_label = r.get("current_label", str(r.get("current")))
            unit = "night" if (bucket or {}).get("period") == "night" else "day"
            revert = {
                "id": uuid.uuid4().hex[:12], "zone": r["zone"], "setting": r["setting"],
                "setting_label": r.get("setting_label", r["setting"]),
                "title": f"{r['zone']}: the last change did not help — revert it",
                "text": (f"After changing {r.get('setting_label', r['setting'])} to {r.get('proposed_label')} the fan was "
                         f"changed by hand on {after} of {after_days} counted {unit}(s) in the {WATCH_DAYS} days after, "
                         f"against {before} of {before_days} in the {WATCH_DAYS} days before. "
                         f"Recommended: go back to {revert_label}."),
                "current": r["proposed"], "proposed": r["current"],
                "current_label": r.get("proposed_label"), "proposed_label": revert_label,
                "evidence": {"before": before, "after": after, "before_days": before_days,
                             "after_days": after_days, "watch_days": WATCH_DAYS},
                "status": "open", "created": now.isoformat(timespec="seconds"),
                "bucket": bucket, "revert_of": r["id"],
            }
            for other in items:
                if other.get("status") == "open" and other["zone"] == r["zone"] and other["setting"] == r["setting"]:
                    self._close(other, "superseded", "the watch of an applied change recommended a revert", now, revert["id"])
            items.append(revert)
            reverts.append(revert)
        return reverts

# Auto Fan Plugin

Intelligent fan speed automation for Indigo 2025.1.

## Quick Start

1. Go to **Auto Fan** to set global defaults (lock duration, weather device, behavior variables)
2. Go to **Zones** to create fan zones
3. Each zone needs: a fan device, temperature sensor(s), and presence sensor(s)
4. Configure the fan speed curve and modifiers per zone

## Fan Speed Curve

Each zone has a single fan speed curve that maps temperature offset from your target to fan speed (0–100%).

### Controls

- **Temperature Range** (±1° to ±5°) — How far from the target temperature the curve extends. Default: ±3°
- **Control Points** (3 to 11, odd only) — How many discrete speed settings across the range. More points = finer control. Default: 7
- **Presets** — Quick-apply curve shapes:
  - **Linear Ramp** — Straight line from 0% to 100%
  - **Aggressive Cooling** — Ramps up fast early, levels off
  - **Gentle Curve** — Stays moderate, accelerates late
  - **Off Until Hot** — Fan off until well above target, then ramps quickly

### How It Works

The chart shows temperature offset on the X axis and fan speed on the Y axis. Drag any control point up or down to set the fan speed at that temperature offset. The plugin linearly interpolates between points at runtime.

**Example**: With range ±3° and 7 control points, offsets are -3°, -2°, -1°, 0°, +1°, +2°, +3°. If the room is 1.5° above target, the speed is interpolated between the +1° and +2° points.

When you change the temperature range or number of control points, existing speed values are smoothly interpolated onto the new grid so the curve shape is preserved.

## Zones

Each zone links a fan device to sensors and defines how the fan responds to conditions.

### Zone Settings

| Setting | Description |
|---------|-------------|
| Fan Device | The fan to control (SpeedControl, Dimmer, or Relay) |
| Temperature Sensors | One or more temp sensors (averaged if multiple) |
| Ideal Temperature | Target temp — fixed value, Indigo variable, or from thermostat setpoints |
| Presence Sensors | Motion/occupancy sensors (any detecting = present) |
| Thermostat | For HVAC mode detection and optional ideal temperature source |
| Humidity Sensors | One or more humidity sensors (averaged if multiple) |
| Night Window | Per-season start and end hour of "night" for this zone (see below) |
| Comfort Offset | Standing degrees added to the ideal, per season and day/night; the learner recommends these |
| Right now (top of the page) | Live strip: fan speed, room and ideal temperature, day/night, lock with Clear; open recommendations with Apply / Dismiss |
| Fan Speed Curve | Interactive chart mapping temperature offset to fan speed |
| Modifiers | Conditional speed adjustments (see below) |

## Modifiers

After the base speed is calculated from the curve, modifiers adjust it in order:

1. **HVAC Cooling** — boost speed when AC is running
2. **HVAC Heating** — reduce speed when heating is active
3. **Humidity** — boost speed when humidity exceeds threshold
4. **Nighttime** — clamp speed to a min/max range while the zone's Night Window is active
5. **Away** — cap speed when nobody is home

Additive modifiers (1–3) run first, then clamps (4–5), so clamps always have final say.

## Night Window

Each zone defines, per season, the hours it counts as night (default 10:00 PM to 8:00 AM). The window is always on; it does not depend on any modifier being enabled. Three things read it:

- the **Nighttime** modifier's speed limits,
- **nighttime locks** (a manual change made at night holds until the window ends),
- the day/night split of **comfort offsets**.

A window may cross midnight. Setting the same hour for start and end means the zone never counts as night.

## Zone Locking

A manual fan speed change (wall switch, remote, Siri, Indigo UI) locks the zone so automation leaves the fan alone. A report that merely confirms the speed the plugin itself commanded is not a manual change.

| Situation | Lock rule |
|-----------|-----------|
| Change inside the zone's Night Window, **Hold Locks at Nighttime** on | Locked until the Night Window ends |
| Any other change | Locked for the Lock Duration; extended by the Lock Extension Duration while presence is detected, up to Maximum Lock hours |

- **Hold Locks at Nighttime** is a plugin-wide default on the Auto Fan page; each zone can follow it, force it on, or force it off.
- A further manual change during a lock restarts it.
- The house going Away releases every lock at once.
- Locks survive plugin restarts and config saves.
- Clear a lock from the plugin menu (*Reset All Zone Locks*), the Indigo actions, or the zone's page.

### Manual change record

Every manual change and every lock release is appended to `state/overrides-YYYY-MM.jsonl` next to the plugin config (one JSON object per line, kept for 400 days). A manual change records the season, day or night, every temperature sensor, the ideal, humidity, HVAC state, presence, outdoor temperature, the step the plugin had set and the step the person chose, and how soon after a lock release it came. A release records why the lock ended and the speed automation resumed at. *Plugins → Auto Fan → Print manual change summary (30 days)* summarizes it per zone, season and day/night. Comfort learning reads this record.

## Comfort Offsets

A comfort offset is a number of degrees added to the zone's configured ideal temperature. The effective ideal is *configured ideal + standing offset + temporary offset*.

**Temporary offset from a manual change.** When a person changes the fan, the plugin runs the season's curve backwards from the step they chose (after removing any active HVAC or humidity boost) to find the ideal temperature they were acting on. The gap to the configured ideal, rounded to 0.5° and capped at ±3°, is held as a temporary offset: until the Night Window ends for a change made at night, until it begins for a change made by day. When the lock ends, automation resumes from that preference.

**By hand.** The "right now" row of the Comfort Offset section applies as soon as you pick a value (no save needed); the Indigo actions *Set Comfort Offset* / *Clear Comfort Offset* do the same from action groups or Siri. Choose until the night window ends or begins, until the Daily Reset Hour (Auto Fan page), or for a number of hours. A hand-set offset replaces a derived one; the next manual change replaces it again.

The zone device shows `configured_ideal_temperature`, `comfort_offset_temporary`, `comfort_offset_expires` and the effective `ideal_temperature`.

## Recommendations (Comfort Learning)

Once a day at the Daily Reset Hour (and from *Plugins → Auto Fan → Run comfort learning now*) the plugin turns the manual change record into recommendations. Nothing changes until you press **Apply** on the zone's page.

| Recommendation | Evidence | Rule |
|----------------|----------|------|
| Standing comfort offset for a season and day/night | the ideal temperature each manual change implied | at least 3 changes on 3 days; evidence halves every 21 days; weighted median; 0.5° steps within ±3° |
| Hold locks at nighttime, or a longer lock duration | lock releases followed by another manual change within the hour | more than 30% of at least 5 releases |

Guardrails: no recommendations during the first 7 days of records; none within 7 days of a change to the same setting; one open recommendation per setting and zone; a hand edit dismisses the open recommendation for that setting. Every applied change is watched for 14 days and a revert is recommended if manual changes did not fall.

Where they show: the Indigo log, the zone device's `open_recommendations` state, the Zones list badge, and a 💡 *Recommendations* block at the top of the zone's page (Apply / Dismiss, with the evidence). *Comfort Learning* can be set to Suggest or Off on the Auto Fan page and overridden per zone.

## HVAC Mode

The plugin auto-detects HVAC mode from thermostat setpoints — no manual season variable needed.

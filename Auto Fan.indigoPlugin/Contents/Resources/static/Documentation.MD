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
| Fan Speed Curve | Interactive chart mapping temperature offset to fan speed |
| Modifiers | Conditional speed adjustments (see below) |

## Modifiers

After the base speed is calculated from the curve, modifiers adjust it in order:

1. **HVAC Cooling** — boost speed when AC is running
2. **HVAC Heating** — reduce speed when heating is active
3. **Humidity** — boost speed when humidity exceeds threshold
4. **Nighttime** — clamp speed to a min/max range during night hours
5. **No Presence** — cap speed when nobody is in the room

Additive modifiers (1–3) run first, then clamps (4–5), so clamps always have final say.

## Zone Locking

Manual fan speed changes (wall switch, Siri, Indigo UI) lock the zone for a configurable duration. While locked, automation won't override the manual setting. Locks extend while presence is detected.

## HVAC Mode

The plugin auto-detects HVAC mode from thermostat setpoints — no manual season variable needed.

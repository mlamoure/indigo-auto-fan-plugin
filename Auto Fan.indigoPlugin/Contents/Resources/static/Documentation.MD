# Auto Fan Plugin

Intelligent fan speed automation for Indigo 2025.1.

## Quick Start

1. Go to **Auto Fan Configuration** to set global defaults (lock duration, weather device, behavior variables)
2. Go to **Zones** to create fan zones
3. Each zone needs: a fan device, temperature sensor(s), and presence sensor(s)
4. Configure speed curves and modifiers per zone

## Speed Curves

Each zone uses two breakpoint curves:

- **Cooling curve** — when the room is warmer than ideal (delta >= 0)
- **Warming curve** — when the room is cooler than ideal (delta < 0)

Define breakpoints as (delta, speed%) pairs. The plugin interpolates linearly between them.

**Example**: With breakpoints (0°, 0%), (3°, 50%), (8°, 100%), a delta of +3° yields 50% fan speed.

## Modifiers

After the base speed is calculated from the curve, modifiers adjust it in order:

1. **HVAC Cooling** — boost speed when AC is running
2. **HVAC Heating** — reduce speed when heating is active
3. **Humidity** — boost speed when humidity exceeds threshold
4. **Nighttime** — clamp speed to a min/max range during night hours
5. **No Presence** — cap speed when nobody is in the room

## Zone Locking

Manual fan speed changes (wall switch, Siri, Indigo UI) lock the zone for a configurable duration. While locked, automation won't override the manual setting. Locks extend while presence is detected.

## HVAC Mode

The plugin auto-detects HVAC mode from thermostat setpoints — no manual season variable needed.

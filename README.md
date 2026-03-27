# Auto Fan - Indigo Plugin

Intelligent fan speed automation for [Indigo 2025.1](https://www.indigodomo.com/). Adjusts ceiling fan speed based on temperature delta, HVAC state, humidity, time of day, and presence detection.

## Features

- **Interactive Fan Speed Curve** — Visual SVG chart editor with draggable control points, configurable temperature range, and preset curve shapes
- **Modifier Stack** — Layered adjustments for HVAC state, humidity, nighttime, and presence that stack on top of the base curve speed
- **HVAC Auto-Detection** — Automatically detects winter/summer/transitional mode from thermostat setpoints (no manual season variable needed)
- **Multi-Sensor Support** — Average multiple temperature sensors per zone
- **Zone Locking** — Manual fan changes lock the zone to prevent automation from overriding, with configurable lock duration and expiration
- **Web Configuration** — Browser-based config editor via Indigo's built-in web server (IWS)
- **Detailed Logging** — Every speed decision is explained with emoji-tagged log entries

## How It Works

Each fan zone defines:

1. **A fan device** (SpeedControl, Dimmer, or Relay)
2. **Temperature sensor(s)** and an **ideal temperature** (fixed, from a variable, or from thermostat setpoints)
3. **A fan speed curve** — maps temperature offset from target to fan speed percentage
4. **Modifiers** — conditional adjustments:
   - HVAC cooling active: boost speed
   - HVAC heating active: reduce speed
   - Humidity above threshold: boost speed
   - Nighttime: clamp speed to a range
   - No presence: cap speed (default: fan off)

### Fan Speed Curve

Each zone has a unified fan curve spanning a symmetric temperature range (±1° to ±5°, default ±3°) with evenly-spaced control points (3 to 11, default 7). The interactive chart editor lets you:

- **Drag points** vertically to set fan speed at each temperature offset
- **Adjust range** to control how far from target the curve extends
- **Change point count** for coarser or finer control
- **Apply presets**: Linear Ramp, Aggressive Cooling, Gentle Curve, Off Until Hot

At runtime, the plugin linearly interpolates between the two nearest control points. Values outside the configured range clamp to the nearest endpoint.

**Example**: With range ±3° and points at 0°→30%, +1°→55%, +2°→80%, a room 1.5° above target yields ~67% fan speed. If HVAC is cooling (+15% modifier), final speed = ~82%.

### Zone Locking

When someone manually changes a fan speed (via wall switch, Indigo UI, Siri, etc.), the zone **locks** for a configurable duration (default: 60 minutes). While locked, automation will not override the manual setting.

- **Lock extension**: If presence is still detected in the zone when the lock is about to expire, the lock extends by the configured extension duration (default: 30 minutes). This keeps the fan at the manual setting as long as the room is occupied.
- **Unlock**: The zone unlocks when the lock expires *and* no presence extension applies. Automation then resumes immediately and sets the fan to the calculated speed.
- **Manual reset**: Locks can be cleared manually via the plugin menu or web interface.

Both lock duration and extension duration can be overridden per-zone.

## Installation

1. Download the latest release zip
2. Double-click to install in Indigo, or copy `Auto Fan.indigoPlugin` to your Indigo Plugins folder
3. Enable the plugin in Indigo's plugin menu
4. Configure zones via the web interface: **Plugins → Auto Fan → Open Web Configuration**

## Configuration

Access the web editor at:
```
http://localhost:8176/message/com.vtmikel.autofan/web_ui/
```

### Plugin Settings

| Setting | Description |
|---------|-------------|
| Default Lock Duration | Minutes a zone stays locked after manual fan change (default: 60) |
| Default Lock Extension | Minutes to extend lock when presence detected (default: 30) |
| Weather Device | Device providing outdoor temperature for HVAC mode detection |
| Global Behavior Variables | Variables that turn all fans off when matched (e.g., nobody home) |

### Zone Settings

| Setting | Description |
|---------|-------------|
| Fan Device | The fan to control (SpeedControl, Dimmer, or Relay) |
| Temperature Sensors | One or more temp sensors (averaged if multiple) |
| Ideal Temperature Source | How to determine the target temperature: **Static** (fixed value), **Variable** (from an Indigo variable), or **Thermostat** (uses heat setpoint, cool setpoint, or average of both) |
| Presence Sensors | Motion/virtual presence devices |
| Thermostat | For HVAC mode auto-detection and optional ideal temperature source |
| Humidity Sensors | One or more humidity sensors for speed boost (averaged if multiple) |
| Fan Speed Curve | Interactive chart mapping temperature offset to fan speed |
| Modifiers | HVAC, nighttime, humidity, presence adjustments |

## Development

### Prerequisites

- Python 3.11+
- Indigo 2025.1

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate.fish  # fish shell
pip install -r requirements.txt
```

### Testing

```bash
python -m pytest tests/ -v
```

### Local Deployment

```bash
python3 /path/to/restart_indigo_plugin.py .
```

### Production Deployment

```bash
/path/to/deploy_indigo_plugin_to_server.sh "Auto Fan.indigoPlugin"
```

## License

MIT

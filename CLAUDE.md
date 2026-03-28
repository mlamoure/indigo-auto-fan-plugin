# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an Indigo Home Automation Fan plugin that provides enhanced fan control capabilities for the Indigo 2025.1 platform.

## Indigo Environment

- **Indigo Version**: 2025.1
- **Logs Directory**: `/Library/Application Support/Perceptive Automation/Indigo 2025.1/Logs`
- **Preferences Directory**: `/Library/Application Support/Perceptive Automation/Indigo 2025.1/Preferences`
- **Plugins Directory**: `/Library/Application Support/Perceptive Automation/Indigo 2025.1/Preferences/Plugins`
- **Local Indigo Installation**: `/usr/local/indigo/`

## Plugin Structure

Standard Indigo plugin structure:
```
<PluginName>.indigoPlugin/
├── Contents/
│   ├── Info.plist                 # Plugin metadata and version
│   ├── Server Plugin/
│   │   ├── plugin.py              # Main plugin class
│   │   ├── requirements.txt       # Python dependencies
│   │   └── ...
│   └── Packages/                  # Auto-generated, excluded from git
```

**CRITICAL**: Always keep the root `requirements.txt` in sync with `<plugin name>.indigoPlugin/Contents/Server Plugin/requirements.txt`.

## Development Commands

### Virtual Environment
Always use the Fish shell virtual environment:
```bash
source .venv/bin/activate.fish
```

Check for virtual environment before running any Python/pip commands.

### Testing Plugin Locally
Restart plugin on local development machine:
```bash
python /Users/mike/Mike_Sync_Documents/Programming/mike-local-development-scripts/restart_indigo_plugin.py .
```

Options:
- `-v` for verbose output
- Can use path to plugin directory, project directory, or bundle identifier

### Deploying to Server
Deploy to production Indigo server for testing:
```bash
/Users/mike/Mike_Sync_Documents/Programming/mike-local-development-scripts/deploy_indigo_plugin_to_server.sh <plugin_folder_name>
```

**REQUIREMENTS**:
- Network transfer takes significant time - consider running in background
- Always restart plugin in Indigo after deployment

### Testing
Place all tests in `tests/` folder. Never place test scripts in repository root.

Always update test cases when functionality changes.

### Releasing to GitHub
Automated GitHub release process:
```bash
/Users/mike/Mike_Sync_Documents/Programming/mike-local-development-scripts/release_indigo_plugin_to_github.sh <plugin_folder_name> -y --notes-file /tmp/release_notes.md
```

**Best Practice Workflow**:
1. Generate comprehensive release notes to `/tmp/release_notes.md`
2. Use `--draft` flag to create draft release for review
3. Use `-y --notes-file` for fully automated releases

Key flags:
- `-y` / `--no-prompt` - Skip interactive prompts
- `--notes-file <path>` - Read release notes from file
- `--draft` - Create as draft release
- `--force-tag` - Recreate tag if exists
- `--dry-run` - Preview without executing

Script automatically:
- Validates no uncommitted changes
- Extracts version from Info.plist
- Creates and pushes git tag
- Packages plugin (removes Contents/Packages and .gitignore files)
- Creates GitHub release with clean zip

### Version Control
Use git-auto-check-in-all alias for automatic commit generation and push.

## Documentation References

**PRIMARY**: Use Indigo 2025.1 documentation (fallback to 2024.2 if unavailable):
- Plugin Development Guide: https://wiki.indigodomo.com/doku.php?id=indigo_2025.1_documentation:plugin_guide
- Object Reference Model: https://wiki.indigodomo.com/doku.php?id=indigo_2025.1_documentation:object_model_reference
- HTTP API: https://wiki.indigodomo.com/doku.php?id=indigo_2025.1_documentation:api#http_api
- Local SDK: `/Users/mike/Mike_Sync_Documents/Programming/IndigoSDK-2025.1`

## Plugin Device Types

Two Indigo device types, both relay-based (on/off = enabled/disabled):

- **`auto_fan_config`** — Singleton global config device. Turning off disables the entire plugin. Dynamic states are populated via `getDeviceStateList()` from `config_field_schemas`.
- **`auto_fan_zone`** — One per zone. Turning off disables that zone's automation. Dynamic states come from `zone_field_schemas` plus runtime states defined in `FanZone.zone_indigo_device_runtime_states`:
  - `current_temperature`, `ideal_temperature`, `temperature_delta`
  - `target_speed_pct`, `current_speed_pct`
  - `presence_detected`
  - `zone_locked`, `lock_expiration`
  - `humidity`, `outdoor_temperature`, `current_season`

### Zone Device Fields

- **`fan_dev_id`** — Single select: SpeedControl, Dimmer, or Relay devices
- **`temp_sensor_dev_ids`** — Multi-select: SensorDevice (averaged if multiple). **Required** — at least one needed for offset calculation.
- **`presence_dev_ids`** — Multi-select: SensorDevice (OR logic — any = present). Optional — when omitted, presence is assumed and fans run based on temperature alone.
- **`thermostat_dev_id`** — Single select: ThermostatDevice
- **`humidity_dev_ids`** — Multi-select: SensorDevice (averaged if multiple)
- **`ideal_temp_source`** — Enum: `"static"` (fixed value), `"variable"` (Indigo variable), `"thermostat"` (uses heat/cool setpoints, or their average when both exist)

## Speed Model

Each zone has **four seasonal fan curves** (spring, summer, fall, winter), each mapping temperature offset from ideal to fan speed (0-100%). The active curve is selected by meteorological season: Spring=Mar-May, Summer=Jun-Aug, Fall=Sep-Nov, Winter=Dec-Feb. Each curve spans a configurable symmetric range (±1° to ±5°, default ±3°) with an odd number of evenly-spaced control points (3 to 11, default 7). Linear interpolation between adjacent points determines the base speed. Values outside the range clamp to the nearest endpoint.

Curves are configured via season tabs in the zone editor, each with an interactive SVG chart editor with draggable points, range/point-count sliders, and preset buttons (Linear Ramp, Aggressive Cooling, Gentle Curve, Off Until Hot).

Data format: `seasonal_curves` object with `spring`, `summer`, `fall`, `winter` keys, each containing `temperature_range`, `num_points`, and `points` array of `{offset, speed}` pairs. Migration chain: legacy `speed_curves` (dual cooling/warming) → `fan_curve` (unified) → `seasonal_curves` (per-season, all four initialized from the single curve).

Base speed from the curve is then passed through a **modifier stack** (order matters):
1. HVAC cooling boost (additive `speed_boost_pct`) + minimum speed clamp
2. HVAC heating adjustment (additive `speed_adjust_pct`, supports +/-) + minimum speed clamp
3. Humidity boost (flat `speed_boost_pct` when above `threshold`)
4. Nighttime clamp (per-season: caps to `[clamp_min_pct, clamp_max_pct]` range using season-specific hours)
5. No-presence cap (caps to `clamp_max_pct`)
6. Final 0-100 clamp

Modifiers use **dropdown-based configuration** with 10% increments. There are no explicit "enabled" flags — modifiers are implicitly disabled at their neutral values (e.g., `speed_boost_pct=0`, `clamp_max_pct=100`). Schema uses `x-enum-labels` for human-friendly dropdown labels.

Additive modifiers run before clamps so that clamps cannot be circumvented by later adjustments.

Migration: legacy `enabled` + numeric fields → dropdown integers. Old `speed_adjust_pct` (cooling) → `speed_boost_pct`. Humidity changed from proportional to flat boost. Values rounded to nearest valid dropdown increment. Nighttime: flat `nighttime` object → per-season `nighttime.{spring,summer,fall,winter}` (all four initialized from the flat values).

## Important Notes

- **MCP Integration**: Use Indigo MCP server tools to interact with Indigo devices, variables, and action groups
- **Clean Releases**: GitHub releases must include clean zip with Contents/Packages and .gitignore files removed (handled automatically by release script)
- **Documentation**: Document implementation details in this CLAUDE.md, user-facing docs in README.md
- **No Extra MD Files**: Only maintain CLAUDE.md and README.md

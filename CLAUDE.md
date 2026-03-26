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
- Indigo Server must be mounted at `/Volumes/Perceptive Automation` (Claude cannot mount - user must do manually)
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
  - `hvac_mode`, `presence_detected`
  - `zone_locked`, `lock_expiration`
  - `humidity`, `outdoor_temperature`

## Speed Model

Each zone uses a **dual-curve** system:
- **Cooling curve** (delta >= 0): maps positive temperature delta to fan speed
- **Warming curve** (delta < 0): maps negative delta to a low circulation speed

Base speed from the selected curve is then passed through a **modifier stack** (order matters):
1. HVAC cooling boost (additive)
2. HVAC heating reduction (additive + clamp)
3. Humidity boost (additive, above threshold)
4. Nighttime clamp (caps to range)
5. No-presence cap (caps to max)
6. Final 0-100 clamp

Additive modifiers run before clamps so that clamps cannot be circumvented by later adjustments.

## Important Notes

- **MCP Integration**: Use Indigo MCP server tools to interact with Indigo devices, variables, and action groups
- **Clean Releases**: GitHub releases must include clean zip with Contents/Packages and .gitignore files removed (handled automatically by release script)
- **Documentation**: Document implementation details in this CLAUDE.md, user-facing docs in README.md
- **No Extra MD Files**: Only maintain CLAUDE.md and README.md

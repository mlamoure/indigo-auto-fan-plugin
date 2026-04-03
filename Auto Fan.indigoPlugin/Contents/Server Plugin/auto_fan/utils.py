try:
    import indigo
except ImportError:
    pass

BAF_PLUGIN_ID = "com.webdeck.indigoplugin.bafcontrol"
BAF_SPEED_COUNT = 8  # BAF fans support speeds 0-7


def is_baf_fan(dev) -> bool:
    """Check if a device is a BAF/Haiku fan with native 8-speed support."""
    return (getattr(dev, "pluginId", "") == BAF_PLUGIN_ID
            and getattr(dev, "deviceTypeId", "") == "bafFan")


def send_fan_speed(fan_dev_id: int, target_speed_pct: float, logger=None) -> bool:
    """
    Set fan speed on an Indigo device.

    Supports BAF/Haiku (8-speed), SpeedControl, Dimmer, and Relay devices.
    For Relay devices, turns on if speed > 0, off if speed == 0.

    Args:
        fan_dev_id: Indigo device ID for the fan.
        target_speed_pct: Target speed percentage (0-100).
        logger: Optional logger for debug output.

    Returns:
        True if speed was set successfully, False otherwise.
    """
    try:
        dev = indigo.devices[fan_dev_id]
    except Exception as e:
        if logger:
            logger.error(f"Cannot find fan device {fan_dev_id}: {e}")
        return False

    target_speed_pct = max(0.0, min(100.0, target_speed_pct))
    speed_int = round(target_speed_pct)

    try:
        # Must check BAF before SpeedControl — BAF fans also have speedLevel
        if is_baf_fan(dev):
            baf_speed = max(0, min(7, round(target_speed_pct * 7 / 100.0)))
            baf_plugin = indigo.server.getPlugin(BAF_PLUGIN_ID)
            if baf_plugin.isEnabled():
                baf_plugin.executeAction(
                    "setBAFFanSpeed",
                    deviceId=fan_dev_id,
                    props={"speed": str(baf_speed)},
                )
            else:
                # BAF plugin disabled — fall back to standard SpeedControl
                if logger:
                    logger.warning(
                        f"BAF plugin disabled, using standard SpeedControl for {dev.name}"
                    )
                indigo.speedcontrol.setSpeedLevel(fan_dev_id, value=speed_int)
        elif hasattr(dev, "speedLevel"):
            indigo.speedcontrol.setSpeedLevel(fan_dev_id, value=speed_int)
        elif hasattr(dev, "brightness"):
            indigo.dimmer.setBrightness(fan_dev_id, value=speed_int)
        else:
            # Relay device (on/off only)
            if speed_int > 0:
                indigo.device.turnOn(fan_dev_id)
            else:
                indigo.device.turnOff(fan_dev_id)

        if logger:
            logger.debug(f"Set fan {dev.name} (id:{fan_dev_id}) to {speed_int}%")
        return True

    except Exception as e:
        if logger:
            logger.error(f"Failed to set fan speed on {dev.name}: {e}")
        return False


def get_fan_speed_pct(fan_dev_id: int) -> float:
    """
    Get current fan speed as a percentage.

    Args:
        fan_dev_id: Indigo device ID for the fan.

    Returns:
        Current speed percentage (0-100), or 0.0 on error.
    """
    try:
        dev = indigo.devices[fan_dev_id]
        # Must check BAF before SpeedControl — BAF fans also have speedLevel
        if is_baf_fan(dev):
            baf_speed = int(dev.states.get("baf_speed", 0))
            return round(baf_speed * 100.0 / (BAF_SPEED_COUNT - 1))
        elif hasattr(dev, "speedLevel"):
            return float(dev.speedLevel)
        elif hasattr(dev, "brightness"):
            return float(dev.brightness)
        elif hasattr(dev, "onState"):
            return 100.0 if dev.onState else 0.0
    except Exception:
        pass
    return 0.0

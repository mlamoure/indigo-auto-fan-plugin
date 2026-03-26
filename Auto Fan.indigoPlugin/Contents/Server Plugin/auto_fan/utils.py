try:
    import indigo
except ImportError:
    pass


def send_fan_speed(fan_dev_id: int, target_speed_pct: float, logger=None) -> bool:
    """
    Set fan speed on an Indigo device.

    Supports SpeedControl devices (native speed level) and Dimmer devices (brightness).
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
        if hasattr(dev, "speedLevel"):
            # SpeedControl device
            if speed_int == 0:
                indigo.speedcontrol.setSpeedLevel(fan_dev_id, value=0)
            else:
                indigo.speedcontrol.setSpeedLevel(fan_dev_id, value=speed_int)
        elif hasattr(dev, "brightness"):
            # Dimmer device (use brightness as speed proxy)
            if speed_int == 0:
                indigo.dimmer.setBrightness(fan_dev_id, value=0)
            else:
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
        if hasattr(dev, "speedLevel"):
            return float(dev.speedLevel)
        elif hasattr(dev, "brightness"):
            return float(dev.brightness)
        elif hasattr(dev, "onState"):
            return 100.0 if dev.onState else 0.0
    except Exception:
        pass
    return 0.0

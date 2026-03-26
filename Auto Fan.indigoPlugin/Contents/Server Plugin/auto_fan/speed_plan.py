from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class SpeedPlan:
    # A list of (emoji, message) explaining WHY we chose this speed
    contributions: List[Tuple[str, str]] = field(default_factory=list)
    # A list of (emoji, message) for exclusions (e.g., zone disabled)
    exclusions: List[Tuple[str, str]] = field(default_factory=list)
    # The target fan speed percentage (0-100)
    target_speed_pct: float = 0.0
    # A list of (emoji, message) describing the change being made
    device_changes: List[Tuple[str, str]] = field(default_factory=list)

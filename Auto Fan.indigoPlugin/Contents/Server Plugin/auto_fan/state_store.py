"""Small JSON/JSONL state store kept beside the plugin config.

Runtime state (locks, comfort offsets, override records, recommendations)
lives in ``<config dir>/state/`` so that it survives plugin restarts and
config reloads, and so that saving the config in the web editor never
touches it.

``StateStore(None)`` keeps everything in memory, which is what the unit
tests use.
"""
import glob
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from typing import Any, Iterator, List, Optional

logger = logging.getLogger("Plugin")


class StateStore:
    def __init__(self, state_dir: Optional[str]) -> None:
        self.state_dir = state_dir
        self._memory: dict = {}
        self._memory_lines: dict = {}
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)

    # ---- JSON documents ----

    def _path(self, name: str) -> str:
        return os.path.join(self.state_dir, f"{name}.json")

    def load(self, name: str, default: Any = None) -> Any:
        """Return the stored document, or ``default`` when absent or unreadable."""
        if not self.state_dir:
            return self._memory.get(name, default)
        path = self._path(name)
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            logger.error(f"State file {path} unreadable, ignoring it: {e}")
            return default

    def save(self, name: str, data: Any) -> None:
        """Write the document atomically (temp file + rename)."""
        if not self.state_dir:
            self._memory[name] = data
            return
        path = self._path(name)
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp, path)
        except OSError as e:
            logger.error(f"Could not write state file {path}: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ---- JSONL logs (one JSON object per line, monthly files) ----

    @staticmethod
    def _month_key(when: datetime) -> str:
        return when.strftime("%Y-%m")

    def append_jsonl(self, name: str, record: dict, when: Optional[datetime] = None) -> None:
        """Append one record to ``<name>-YYYY-MM.jsonl``."""
        when = when or datetime.now()
        key = f"{name}-{self._month_key(when)}"
        line = json.dumps(record, default=str)
        if not self.state_dir:
            self._memory_lines.setdefault(key, []).append(line)
            return
        path = os.path.join(self.state_dir, f"{key}.jsonl")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            logger.error(f"Could not append to {path}: {e}")

    def iter_jsonl(self, name: str) -> Iterator[dict]:
        """Yield every record of every ``<name>-*.jsonl`` file, oldest file first."""
        if not self.state_dir:
            for key in sorted(k for k in self._memory_lines if k.startswith(f"{name}-")):
                for line in self._memory_lines[key]:
                    yield json.loads(line)
            return
        for path in sorted(glob.glob(os.path.join(self.state_dir, f"{name}-*.jsonl"))):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            yield json.loads(line)
                        except ValueError:
                            logger.warning(f"Skipping unreadable line in {path}")
            except OSError as e:
                logger.error(f"Could not read {path}: {e}")

    def prune_jsonl(self, name: str, keep_days: int, now: Optional[datetime] = None) -> List[str]:
        """Delete monthly files whose month ended more than ``keep_days`` ago."""
        now = now or datetime.now()
        cutoff = now - timedelta(days=keep_days)
        removed = []
        if not self.state_dir:
            for key in list(self._memory_lines):
                if key.startswith(f"{name}-") and self._month_is_before(key[len(name) + 1:], cutoff):
                    del self._memory_lines[key]
                    removed.append(key)
            return removed
        for path in glob.glob(os.path.join(self.state_dir, f"{name}-*.jsonl")):
            month = os.path.basename(path)[len(name) + 1:-len(".jsonl")]
            if self._month_is_before(month, cutoff):
                try:
                    os.unlink(path)
                    removed.append(path)
                except OSError as e:
                    logger.error(f"Could not remove {path}: {e}")
        return removed

    @staticmethod
    def _month_is_before(month: str, cutoff: datetime) -> bool:
        """True when every day of ``month`` (YYYY-MM) is before ``cutoff``."""
        try:
            year, mon = int(month[:4]), int(month[5:7])
        except ValueError:
            return False
        if mon == 12:
            first_of_next = datetime(year + 1, 1, 1)
        else:
            first_of_next = datetime(year, mon + 1, 1)
        return first_of_next <= cutoff

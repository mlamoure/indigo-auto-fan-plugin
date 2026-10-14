"""Write the plugin config file the same way the web editor does: an
automatic backup first, then the new JSON, keeping the newest 20 backups."""
import glob
import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict

AUTO_BACKUP_KEEP = 20


def load_config(config_path: str) -> Dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config_with_backup(config_path: str, data: Dict[str, Any]) -> str:
    """Back up the current file to <config dir>/auto_backups/, write ``data``,
    prune old backups. Returns the backup path (or "" when nothing existed)."""
    config_dir = os.path.dirname(os.path.abspath(config_path))
    backup_dir = os.path.join(config_dir, "auto_backups")
    os.makedirs(backup_dir, exist_ok=True)
    backup = ""
    if os.path.exists(config_path):
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = os.path.join(backup_dir, f"auto_backup_{stamp}.json")
        shutil.copy2(config_path, backup)
        old = sorted(glob.glob(os.path.join(backup_dir, "auto_backup_*.json")))
        for path in old[:-AUTO_BACKUP_KEEP]:
            try:
                os.unlink(path)
            except OSError:
                pass
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, config_path)
    return backup

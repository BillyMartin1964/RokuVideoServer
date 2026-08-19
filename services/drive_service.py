import os
import shutil
import config
from config import VOLUMES_DIR, IGNORED_DIRS, CACHE_LOCK, log


def get_indexed_drive_thumbnail(vol_name: str) -> str:
    """Finds the first indexed thumbnail ID for a volume without holding the lock too long."""
    with CACHE_LOCK:
        for item in config.FILES_LIST:
            if item.get("drive") == vol_name and item.get("id"):
                return item["id"]
    return ""


def get_system_drives() -> list:
    """Reads volumes directory and returns raw drive metadata list."""
    drive_list = []

    if not os.path.exists(VOLUMES_DIR):
        return drive_list

    try:
        for vol_name in os.listdir(VOLUMES_DIR):
            if vol_name.startswith(".") or vol_name.lower() in IGNORED_DIRS:
                continue

            if vol_name == "Macintosh HD":
                continue

            vol_path = os.path.join(VOLUMES_DIR, vol_name)

            try:
                if not os.path.exists(vol_path) or not os.path.isdir(vol_path):
                    continue
            except OSError as ex:
                log(f"<!> Skipping inaccessible volume target '{vol_name}': {ex}")
                continue

            drive_info = {
                "drive": vol_name,
                "name": vol_name,
                "title": vol_name,
                "path": vol_path,
                "thumbUrl": "",
                "totalBytes": 0,
                "freeBytes": 0,
            }

            try:
                usage = shutil.disk_usage(vol_path)
                drive_info["totalBytes"] = usage.total
                drive_info["freeBytes"] = usage.free
            except (PermissionError, FileNotFoundError, OSError) as ex:
                log(f"<!> Could not fetch disk usage for '{vol_name}': {ex}")

            thumb_id = get_indexed_drive_thumbnail(vol_name)
            if thumb_id:
                drive_info["thumbUrl"] = f"/api/thumbnails/{thumb_id}"

            drive_list.append(drive_info)

    except Exception as ex:
        log(f"<!> Error reading drives from {VOLUMES_DIR}: {type(ex).__name__}: {ex}")

    drive_list.sort(key=lambda x: x["drive"].lower())
    return drive_list
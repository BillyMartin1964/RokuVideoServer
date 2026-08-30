#!/usr/bin/env python3

import json
import os
import shutil
from pathlib import Path

import config
from config import CACHE_LOCK, IGNORED_DIRS, VOLUMES_DIR, log

CONFIG_FILE = Path("authorized_drives.json")


# ============================================================================
# AUTHORIZED DRIVES PERSISTENCE
# ============================================================================


def get_authorized_drives() -> set[str]:
    """
    Read authorized drive names from authorized_drives.json.

    Returns a set of drive names (e.g., {"Vids1", "Vids2"}).
    If the file does not exist or is invalid, returns an empty set.
    """
    if not CONFIG_FILE.exists():
        return set()

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            drives = data.get("authorized_drives", [])
            return {
                drive.strip()
                for drive in drives
                if isinstance(drive, str) and drive.strip()
            }
    except (OSError, json.JSONDecodeError, ValueError) as ex:
        log(f"<!> Failed to read authorized_drives.json: {ex}")
        return set()


def save_authorized_drives(drives: list[str]) -> bool:
    """
    Save a list of authorized drive names to authorized_drives.json.
    """
    try:
        clean_drives = sorted(
            {
                drive.strip()
                for drive in drives
                if isinstance(drive, str) and drive.strip()
            }
        )
        payload = {"authorized_drives": clean_drives}

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        log(
            f"[*] Successfully updated authorized_drives.json with {len(clean_drives)} drive(s)."
        )
        return True
    except (OSError, TypeError, ValueError) as ex:
        log(f"<!> Failed to write authorized_drives.json: {ex}")
        return False


def is_drive_authorized(drive_name: str) -> bool:
    """
    Check whether a specific drive name is authorized in authorized_drives.json.
    """
    if not drive_name or not drive_name.strip():
        return False

    authorized = get_authorized_drives()
    return drive_name.strip() in authorized


# ============================================================================
# SYSTEM DRIVE INSPECTION & METADATA
# ============================================================================


def get_indexed_drive_thumbnail(vol_name: str) -> str:
    """Finds the first indexed thumbnail ID for a volume without holding the lock too long."""
    with CACHE_LOCK:
        for item in getattr(config, "FILES_LIST", []):
            if item.get("drive") == vol_name and item.get("id"):
                return item["id"]
    return ""


def get_system_drives(only_authorized: bool = False) -> list[dict]:
    """
    Reads volumes directory and returns raw drive metadata list.

    Includes an 'authorized' boolean flag on every item.
    If only_authorized is True, filters out drives where authorized is False.
    """
    drive_list = []

    if not os.path.exists(VOLUMES_DIR):
        return drive_list

    authorized_set = get_authorized_drives()

    try:
        for vol_name in os.listdir(VOLUMES_DIR):
            if vol_name.startswith(".") or vol_name.lower() in IGNORED_DIRS:
                continue

            if vol_name == "Macintosh HD":
                continue

            is_auth = vol_name in authorized_set

            # Filter if caller requested only authorized drives
            if only_authorized and not is_auth:
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
                "authorized": is_auth,
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
                drive_info["thumbUrl"] = f"/api/video-models/{thumb_id}/thumbnail"

            drive_list.append(drive_info)

    except (OSError, PermissionError, FileNotFoundError) as ex:
        log(f"<!> Error reading drives from {VOLUMES_DIR}: {type(ex).__name__}: {ex}")

    drive_list.sort(key=lambda x: x["drive"].lower())
    return drive_list


def get_drives_response(only_authorized: bool = False) -> dict:
    """Return wrapped JSON structure for drives API."""
    drives = get_system_drives(only_authorized=only_authorized)
    return {
        "success": True,
        "data": drives,  # ✅ Added for Roku BrightScript
        "drives": drives,  # ✅ Kept for Swift UI
        "total": len(drives),  # ✅ Added for Roku BrightScript
        "count": len(drives),  # ✅ Kept for Swift UI
    }

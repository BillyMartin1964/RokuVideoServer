import json
import os
import shutil
from pathlib import Path

import config
from config import CACHE_LOCK, IGNORED_DIRS, VOLUMES_DIR

CONFIG_FILE = Path("authorized_drives.json")


# ============================================================================
# AUTHORIZED DRIVES PERSISTENCE
# ============================================================================


def get_authorized_drives() -> set[str]:
    """Read authorized drive names from authorized_drives.json."""
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
        config.log(f"<!> Failed to read authorized_drives.json: {ex}")
        return set()


def save_authorized_drives(drives: list[str]) -> bool:
    """Save a list of authorized drive names to authorized_drives.json."""
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

        config.log(
            f"[*] Successfully updated authorized_drives.json with {len(clean_drives)} drive(s)."
        )
        return True
    except (OSError, TypeError, ValueError) as ex:
        config.log(f"<!> Failed to write authorized_drives.json: {ex}")
        return False


# ============================================================================
# HTTP HANDLERS
# ============================================================================


def handle_get_drives(handler, include_all: bool = False):
    """
    Fetch USB Drives metadata.

    Returns both 'data'/'total' (for Roku) and 'drives'/'count' (for Swift).
    If include_all is False, filters list to only include authorized drives.
    """
    drive_list = []
    authorized_set = get_authorized_drives()

    if os.path.exists(VOLUMES_DIR):
        try:
            for vol_name in os.listdir(VOLUMES_DIR):
                if vol_name.startswith(".") or vol_name.lower() in IGNORED_DIRS:
                    continue

                # Exclude local startup disk
                if vol_name == "Macintosh HD":
                    continue

                is_auth = vol_name in authorized_set

                # If include_all is False, return only authorized drives
                if not include_all and not is_auth:
                    continue

                vol_path = os.path.join(VOLUMES_DIR, vol_name)

                if os.path.isdir(vol_path):
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

                    # Fetch drive capacity metadata
                    try:
                        usage = shutil.disk_usage(vol_path)
                        drive_info["totalBytes"] = usage.total
                        drive_info["freeBytes"] = usage.free
                    except Exception:
                        pass

                    # Grab a poster thumbnail from indexed cache if available
                    with CACHE_LOCK:
                        for item in getattr(config, "FILES_LIST", []):
                            if item.get("drive") == vol_name and item.get("id"):
                                drive_info["thumbUrl"] = f"/api/thumbnails/{item['id']}"
                                break

                    drive_list.append(drive_info)

        except Exception as ex:
            config.log(
                f"<!> Error reading drives from {VOLUMES_DIR}: {type(ex).__name__}: {ex}"
            )

    drive_list.sort(key=lambda x: x["drive"].lower())

    handler.send_json_response(
        {
            "success": True,
            "total": len(drive_list),
            "count": len(drive_list),
            "data": drive_list,  # Roku key
            "drives": drive_list,  # Swift key
        }
    )


def handle_set_authorized_drives(handler, body: dict):
    """
    Save list of authorized drives sent from the Swift app.

    Accepts payload keys: 'authorized_drives' or 'drives'.
    """
    drives = body.get("authorized_drives") or body.get("drives") or []

    if not isinstance(drives, list):
        handler.send_json_response(
            {
                "success": False,
                "error": "Invalid format. Expected array of drive names.",
            },
            status=400,
        )
        return

    success = save_authorized_drives(drives)
    handler.send_json_response(
        {
            "success": success,
            "message": (
                "Authorized drives saved successfully."
                if success
                else "Failed to save authorized drives."
            ),
        },
        status=200 if success else 500,
    )

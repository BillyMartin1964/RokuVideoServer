import os
import shutil
import config
from config import VOLUMES_DIR, IGNORED_DIRS, CACHE_LOCK


def handle_get_drives(handler):
    drive_list = []

    if os.path.exists(VOLUMES_DIR):
        try:
            for vol_name in os.listdir(VOLUMES_DIR):
                if vol_name.startswith(".") or vol_name.lower() in IGNORED_DIRS:
                    continue
                
                # Exclude local startup disk if desired
                if vol_name == "Macintosh HD":
                    continue

                vol_path = os.path.join(VOLUMES_DIR, vol_name)

                if os.path.isdir(vol_path):
                    drive_info = {
                        "drive": vol_name,
                        "name": vol_name,
                        "title": vol_name,
                        "path": vol_path,
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
                        for item in config.FILES_LIST:
                            if item.get("drive") == vol_name and item.get("id"):
                                drive_info["thumbUrl"] = f"/api/thumbnails/{item['id']}"
                                break

                    drive_list.append(drive_info)

        except Exception as ex:
            config.log(f"<!> Error reading drives from {VOLUMES_DIR}: {type(ex).__name__}: {ex}")

    drive_list.sort(key=lambda x: x["drive"].lower())

    handler.send_json_response(
        {
            "success": True,
            "total": len(drive_list),
            "data": drive_list,
        }
    )
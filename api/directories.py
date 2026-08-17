import config
from config import CACHE_LOCK


def handle_get_all_directories(handler):
    dirs = {}
    with CACHE_LOCK:
        for item in config.FILES_LIST:
            drive = item.get("drive", "")
            sub = item.get("subfolder", "")
            key = f"{drive}|{sub}"

            if key not in dirs:
                thumb_id = item.get("id", "")
                thumb_url = f"/api/thumbnails/{thumb_id}" if thumb_id else ""
                sub_display = sub.lstrip("/") if isinstance(sub, str) else sub

                if sub_display:
                    display_name = f"{drive} / {sub_display}"
                    title_only = sub_display.split("/")[-1]
                else:
                    display_name = drive
                    title_only = drive

                dirs[key] = {
                    "drive": drive,
                    "subfolder": sub,
                    "dirKey": key,
                    "thumbUrl": thumb_url,
                    "name": display_name,
                    "title": title_only,
                }

    directory_list = list(dirs.values())
    handler.send_json_response(
        {"success": True, "total": len(directory_list), "data": directory_list}
    )


def handle_get_directories_by_drive(handler, drive_name):
    dirs = {}
    target_drive_lower = drive_name.lower().strip()

    with CACHE_LOCK:
        for item in config.FILES_LIST:
            drive = item.get("drive", "")

            # Filter out entries that do not match the target drive
            if drive.lower().strip() != target_drive_lower:
                continue

            sub = item.get("subfolder", "")
            key = f"{drive}|{sub}"

            if key not in dirs:
                thumb_id = item.get("id", "")
                thumb_url = f"/api/thumbnails/{thumb_id}" if thumb_id else ""
                sub_display = sub.lstrip("/") if isinstance(sub, str) else sub

                if sub_display:
                    display_name = f"{drive} / {sub_display}"
                    title_only = sub_display.split("/")[-1]
                else:
                    display_name = drive
                    title_only = drive

                dirs[key] = {
                    "drive": drive,
                    "subfolder": sub,
                    "dirKey": key,
                    "thumbUrl": thumb_url,
                    "name": display_name,
                    "title": title_only,
                }

    directory_list = list(dirs.values())
    handler.send_json_response(
        {
            "success": True,
            "drive": drive_name,
            "total": len(directory_list),
            "data": directory_list,
        }
    )
import hashlib
import json
import os
import subprocess
import sys
import time

# Ensure project root (~/Documents/RokuVideoServer/) is in sys.path for absolute imports
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
from config import (
    ALLOWED_EXTENSIONS,
    CACHE_LOCK,
    FILE_CACHE_FILE,
    IGNORED_DIRS,
    IGNORED_EXTENSIONS,
    REFRESH_INTERVAL_SECONDS,
    VIDEO_FORMATS,
    VOLUMES_DIR,
    log,
)
from models.video_model import create_video_model
from services import ffmpeg_service


def get_file_id(full_path: str) -> str:
    """Generates a deterministic unique ID based on the normalized file path."""
    normalized_path = os.path.abspath(full_path).lower()
    return hashlib.md5(normalized_path.encode("utf-8")).hexdigest()


def get_video_format_info(file_path: str) -> dict:
    extension = os.path.splitext(file_path)[1].lower()
    info = VIDEO_FORMATS.get(extension)
    if info:
        return {
            "extension": extension,
            "streamFormat": info["streamFormat"],
            "contentType": info["contentType"],
        }
    return {
        "extension": extension,
        "streamFormat": "mp4",
        "contentType": "application/octet-stream",
    }


def add_media_metadata(item_data: dict, file_path: str):
    format_info = get_video_format_info(file_path)
    item_data["ext"] = format_info["extension"]
    item_data["extension"] = format_info["extension"]
    item_data["streamFormat"] = format_info["streamFormat"]
    item_data["contentType"] = format_info["contentType"]

    metadata = ffmpeg_service.probe_video_metadata(file_path)
    for key, value in metadata.items():
        item_data[key] = value


def load_disk_cache():
    if not os.path.exists(FILE_CACHE_FILE):
        log("--> No existing video catalog cache found.")
        return

    try:
        with open(FILE_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            return

        normalized_list = []
        normalized_map = {}

        for item in data:
            if isinstance(item, dict):
                # Standardize through VideoModel contract
                model_dict = create_video_model(item).model_dump()
                file_id = model_dict.get("id")
                if file_id:
                    normalized_list.append(model_dict)
                    normalized_map[file_id] = model_dict

        with CACHE_LOCK:
            config.FILES_LIST = normalized_list
            config.FILE_MAP = normalized_map

        log(f"--> Loaded {len(config.FILES_LIST)} indexed videos from SSD cache.")
    except (OSError, json.JSONDecodeError, ValueError) as ex:
        log(f"<!> Error reading video catalog cache: {type(ex).__name__}: {ex}")


def save_disk_cache():
    try:
        with CACHE_LOCK:
            data = list(config.FILES_LIST)

        temp_file = FILE_CACHE_FILE + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        os.replace(temp_file, FILE_CACHE_FILE)
        log("--> Updated SSD disk cache file.")
    except (OSError, TypeError, ValueError) as ex:
        log(f"<!> Error writing SSD disk cache: {type(ex).__name__}: {ex}")


def try_spotlight_index_scan():
    query = " || ".join([f"kMDItemFSName == '*{ext}'" for ext in ALLOWED_EXTENSIONS])
    cmd = ["mdfind", "-onlyin", VOLUMES_DIR, query]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        paths = [p.strip() for p in result.stdout.splitlines() if p.strip()]

        new_list, new_map = [], {}
        for full_path in paths:
            path_lower = full_path.lower()
            if any(part.startswith(".") for part in full_path.split("/")):
                continue
            if any(ignored in path_lower for ignored in IGNORED_DIRS):
                continue
            if any(ignored_ext in path_lower for ignored_ext in IGNORED_EXTENSIONS):
                continue
            if not os.path.exists(full_path):
                continue

            file_id = get_file_id(full_path)
            file_name = os.path.basename(full_path)
            parts = full_path.replace(VOLUMES_DIR, "").strip("/").split("/")
            if not parts:
                continue

            drive_name = parts[0]
            rel_dir = (
                os.path.dirname(full_path).split(drive_name, 1)[-1].replace("\\", "/")
            )
            subfolder = (
                rel_dir if not rel_dir or rel_dir.startswith("/") else "/" + rel_dir
            )

            try:
                file_size = os.path.getsize(full_path)
            except OSError:
                file_size = 0

            raw_item = {
                "id": file_id,
                "fileId": file_id,
                "name": os.path.splitext(file_name)[0],
                "title": os.path.splitext(file_name)[0],
                "drive": drive_name,
                "directory": subfolder,
                "subfolder": subfolder,
                "fullPath": full_path,
                "path": full_path,
                "size": file_size,
            }
            add_media_metadata(raw_item, full_path)

            model_dict = create_video_model(raw_item).model_dump()
            new_list.append(model_dict)
            new_map[file_id] = model_dict

        if new_list:
            return new_list, new_map
    except (subprocess.SubprocessError, OSError, ValueError) as ex:
        log(f"<!> Spotlight scan exception: {type(ex).__name__}: {ex}")
    return None, None


def safe_scan_directory(
    current_dir,
    drive_name,
    volume_path,
    results_list,
    results_map,
    depth=0,
):
    if depth > 20:
        return
    try:
        with os.scandir(current_dir) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                entry_lower = entry.name.lower()
                ext = os.path.splitext(entry_lower)[1]
                if ext in IGNORED_EXTENSIONS or entry_lower in IGNORED_DIRS:
                    continue

                full_path = entry.path
                if entry.is_dir(follow_symlinks=False):
                    safe_scan_directory(
                        full_path,
                        drive_name,
                        volume_path,
                        results_list,
                        results_map,
                        depth + 1,
                    )
                elif entry.is_file(follow_symlinks=False):
                    if ext not in ALLOWED_EXTENSIONS:
                        continue
                    file_id = get_file_id(full_path)
                    rel_path = current_dir[len(volume_path) :].replace("\\", "/")
                    subfolder = (
                        rel_path
                        if not rel_path or rel_path.startswith("/")
                        else "/" + rel_path
                    )

                    try:
                        file_size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        file_size = 0

                    raw_item = {
                        "id": file_id,
                        "fileId": file_id,
                        "name": os.path.splitext(entry.name)[0],
                        "title": os.path.splitext(entry.name)[0],
                        "drive": drive_name,
                        "directory": subfolder,
                        "subfolder": subfolder,
                        "fullPath": full_path,
                        "path": full_path,
                        "size": file_size,
                    }
                    add_media_metadata(raw_item, full_path)

                    model_dict = create_video_model(raw_item).model_dump()
                    results_list.append(model_dict)
                    results_map[file_id] = model_dict
    except (PermissionError, OSError) as ex:
        log(f"<!> Directory scan access warning for {current_dir}: {ex}")


def run_catalog_scan():
    if config.SCAN_IN_PROGRESS:
        return
    config.SCAN_IN_PROGRESS = True
    start_time = time.time()

    try:
        new_list, new_map = try_spotlight_index_scan()

        if new_list is None or len(new_list) == 0:
            new_list, new_map = [], {}
            if os.path.exists(VOLUMES_DIR):
                try:
                    for vol_name in os.listdir(VOLUMES_DIR):
                        if vol_name.startswith(".") or vol_name == "Macintosh HD":
                            continue
                        vol_path = os.path.join(VOLUMES_DIR, vol_name)
                        if os.path.isdir(vol_path):
                            safe_scan_directory(
                                vol_path,
                                vol_name,
                                vol_path,
                                new_list,
                                new_map,
                            )
                except OSError as ex:
                    log(f"<!> Error reading /Volumes: {type(ex).__name__}: {ex}")

        with CACHE_LOCK:
            config.FILES_LIST = new_list
            config.FILE_MAP = new_map

        save_disk_cache()
        elapsed = round(time.time() - start_time, 2)
        log(
            f"--> Catalog scan complete in {elapsed}s. Total indexed videos: {len(new_list)}"
        )
    finally:
        config.SCAN_IN_PROGRESS = False


def background_timer_loop():
    run_catalog_scan()
    while True:
        time.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            run_catalog_scan()
        except (OSError, RuntimeError, ValueError) as ex:
            log(f"<!> Background catalog scan error: {type(ex).__name__}: {ex}")
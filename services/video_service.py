import os
import json
import time
import hashlib
import subprocess
import config
from config import (
    log,
    CACHE_LOCK,
    FILE_CACHE_FILE,
    VOLUMES_DIR,
    ALLOWED_EXTENSIONS,
    IGNORED_DIRS,
    IGNORED_EXTENSIONS,
    VIDEO_FORMATS,
    REFRESH_INTERVAL_SECONDS,
)
import services.ffmpeg_service as ffmpeg_service


def get_file_id(full_path):
    return hashlib.md5(full_path.encode("utf-8")).hexdigest()


def get_video_format_info(file_path):
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


def add_media_metadata(item_data, file_path):
    format_info = get_video_format_info(file_path)
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

        with CACHE_LOCK:
            config.FILES_LIST = data
            config.FILE_MAP = {
                item["id"]: item
                for item in data
                if isinstance(item, dict) and "id" in item
            }
        log(
            f"--> Loaded {len(config.FILES_LIST)} indexed videos from SSD cache."
        )
    except Exception as ex:
        log(f"<!> Error reading video catalog cache: {type(ex).__name__}: {ex}")


def save_disk_cache():
    try:
        with CACHE_LOCK:
            data = list(config.FILES_LIST)

        cache_dir = os.path.dirname(FILE_CACHE_FILE)
        if cache_dir and not os.path.exists(cache_dir):
            os.makedirs(cache_dir, exist_ok=True)

        temp_file = FILE_CACHE_FILE + ".tmp"
        
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        try:
            os.replace(temp_file, FILE_CACHE_FILE)
        except OSError:
            with open(FILE_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            if os.path.exists(temp_file):
                os.remove(temp_file)

        log("--> Updated SSD disk cache file.")
    except Exception as ex:
        log(f"<!> Error writing SSD disk cache: {type(ex).__name__}: {ex}")


def try_spotlight_index_scan():
    query = " || ".join(
        [f"kMDItemFSName == '*{ext}'" for ext in ALLOWED_EXTENSIONS]
    )
    cmd = ["mdfind", "-onlyin", VOLUMES_DIR, query]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
        paths = [
            p.strip() for p in result.stdout.splitlines() if p.strip()
        ]

        new_list, new_map = [], {}
        for full_path in paths:
            path_lower = full_path.lower()
            if any(
                part.startswith(".") for part in full_path.split("/")
            ):
                continue
            if any(ignored in path_lower for ignored in IGNORED_DIRS):
                continue
            if any(
                ignored_ext in path_lower
                for ignored_ext in IGNORED_EXTENSIONS
            ):
                continue
            if not os.path.exists(full_path):
                continue

            file_id = get_file_id(full_path)
            file_name = os.path.basename(full_path)
            parts = (
                full_path.replace(VOLUMES_DIR, "").strip("/").split("/")
            )
            if not parts:
                continue

            drive_name = parts[0]
            rel_dir = (
                os.path.dirname(full_path)
                .split(drive_name, 1)[-1]
                .replace("\\", "/")
            )
            subfolder = (
                rel_dir
                if not rel_dir or rel_dir.startswith("/")
                else "/" + rel_dir
            )

            try:
                file_size = os.path.getsize(full_path)
            except Exception:
                file_size = 0

            item_data = {
                "id": file_id,
                "name": os.path.splitext(file_name)[0],
                "drive": drive_name,
                "subfolder": subfolder,
                "path": full_path,
                "size": file_size,
            }
            add_media_metadata(item_data, full_path)
            new_list.append(item_data)
            new_map[file_id] = item_data

        if new_list:
            return new_list, new_map
    except Exception:
        pass
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
                    rel_path = current_dir[len(volume_path) :].replace(
                        "\\", "/"
                    )
                    subfolder = (
                        rel_path
                        if not rel_path or rel_path.startswith("/")
                        else "/" + rel_path
                    )

                    try:
                        file_size = entry.stat(
                            follow_symlinks=False
                        ).st_size
                    except Exception:
                        file_size = 0

                    item_data = {
                        "id": file_id,
                        "name": os.path.splitext(entry.name)[0],
                        "drive": drive_name,
                        "subfolder": subfolder,
                        "path": full_path,
                        "size": file_size,
                    }
                    add_media_metadata(item_data, full_path)
                    results_list.append(item_data)
                    results_map[file_id] = item_data
    except (PermissionError, OSError):
        pass


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
                        if (
                            vol_name.startswith(".")
                            or vol_name == "Macintosh HD"
                        ):
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
                except Exception as ex:
                    log(
                        f"<!> Error reading /Volumes: {type(ex).__name__}: {ex}"
                    )

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
        except Exception as ex:
            log(f"<!> Background catalog scan error: {type(ex).__name__}: {ex}")

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid

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
    VIDEO_FORMATS,
    VOLUMES_DIR,
    log,
    log_separator,
)
from models.video_model import create_video_model
from services import ffmpeg_service, trickplay_service


def get_file_id(full_path: str) -> str:
    """Reuse a catalog ID for known paths; assign a UUID to new videos."""
    normalized_path = os.path.abspath(full_path).lower()
    known_id = config.PATH_ID_MAP.get(normalized_path)
    if known_id:
        return known_id
    if not os.path.exists(full_path):
        # A deletion event for an unknown path must not create an identity.
        return ""
    with CACHE_LOCK:
        return config.PATH_ID_MAP.setdefault(normalized_path, uuid.uuid4().hex)


def catalog_path_ids(items: list[dict]) -> dict[str, str]:
    return {
        os.path.abspath(path).lower(): item["id"]
        for item in items
        if isinstance(item, dict)
        if isinstance(item.get("id"), str) and item["id"]
        if (path := item.get("path") or item.get("fullPath"))
    }


def catalog_path(item: dict) -> str:
    path = item.get("fullPath") or item.get("path") or ""
    return os.path.abspath(path).lower() if path else ""


def deduplicate_catalog(items: list[dict]) -> list[dict]:
    """Keep one model per physical path, favoring its existing media assets."""
    chosen: dict[str, dict] = {}
    order: list[str] = []

    def score(item: dict) -> int:
        file_id = item.get("id") or ""
        return (
            8 * os.path.isdir(os.path.join(config.TRICKPLAY_CACHE_DIR, file_id))
            + 4 * os.path.isfile(os.path.join(config.THUMB_CACHE_DIR, f"{file_id}.jpg"))
            + 2 * bool(item.get("bookmarkPosition"))
            + bool(item.get("streamUrl"))
        )

    for item in items:
        path = catalog_path(item)
        if not path:
            continue
        if path not in chosen:
            chosen[path] = item
            order.append(path)
        elif score(item) >= score(chosen[path]):
            chosen[path] = item

    return [chosen[path] for path in order]


def video_fingerprint(path: str) -> str:
    """Sample three parts of a video to identify a move without reading it all."""
    size = os.path.getsize(path)
    digest = hashlib.sha256(str(size).encode("ascii"))
    sample_size = 256 * 1024
    with open(path, "rb") as video:
        for offset in (0, max(0, size // 2 - sample_size // 2), max(0, size - sample_size)):
            video.seek(offset)
            digest.update(video.read(sample_size))
    if os.path.getsize(path) != size:
        raise OSError("Video changed while fingerprinting")
    return digest.hexdigest()


def get_file_size(full_path: str) -> int:
    """Returns the file size in bytes using a filesystem fallback if needed."""
    try:
        file_size = os.path.getsize(full_path)

        if file_size > 0:
            return file_size

    except OSError:
        pass

    try:
        result = subprocess.run(
            ["stat", "-f", "%z", full_path],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        if result.returncode == 0:
            file_size = int(result.stdout.strip())

            if file_size > 0:
                return file_size

    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    return 0


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
    try:
        item_data["contentFingerprint"] = video_fingerprint(file_path)
    except OSError as ex:
        log(f"<!> Fingerprint unavailable for {file_path}: {ex}")
    format_info = get_video_format_info(file_path)

    item_data["ext"] = format_info["extension"]
    item_data["extension"] = format_info["extension"]
    item_data["streamFormat"] = format_info["streamFormat"]
    item_data["contentType"] = format_info["contentType"]

    metadata = ffmpeg_service.probe_video_metadata(file_path)

    for key, value in metadata.items():
        item_data[key] = value


def load_disk_cache():
    load_missing_cache()
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
        fingerprints_added = False

        for item in data:
            if isinstance(item, dict):
                # Standardize through VideoModel contract
                model_dict = create_video_model(item).model_dump()
                cached_path = model_dict.get("fullPath") or ""
                if not model_dict.get("contentFingerprint") and cached_path and os.path.isfile(cached_path):
                    try:
                        model_dict["contentFingerprint"] = video_fingerprint(cached_path)
                        fingerprints_added = True
                    except OSError:
                        pass

                if model_dict.get("fileSize", 0) == 0:
                    full_path = model_dict.get("fullPath") or model_dict.get("path")

                    if full_path:
                        file_size = get_file_size(full_path)

                        if file_size > 0:
                            model_dict["fileSize"] = file_size

                file_id = model_dict.get("id")

                if file_id:
                    normalized_list.append(model_dict)
                    normalized_map[file_id] = model_dict

        original_count = len(normalized_list)
        normalized_list = deduplicate_catalog(normalized_list)
        normalized_map = {item["id"]: item for item in normalized_list}

        with CACHE_LOCK:
            config.FILES_LIST = normalized_list
            config.FILE_MAP = normalized_map
            config.PATH_ID_MAP = catalog_path_ids(normalized_list)

        log(f"--> Loaded {len(config.FILES_LIST)} indexed videos from SSD cache.")
        if fingerprints_added or len(normalized_list) != original_count:
            save_disk_cache()

    except (OSError, json.JSONDecodeError, ValueError) as ex:
        log(f"<!> Error reading video catalog cache: {type(ex).__name__}: {ex}")


def load_missing_cache():
    try:
        with open(config.MISSING_CACHE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, dict):
            with CACHE_LOCK:
                config.MISSING_VIDEOS = {
                    key: value for key, value in data.items()
                    if isinstance(key, str) and isinstance(value, dict)
                    and not os.path.exists(value.get("fullPath") or value.get("path") or "")
                }
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as ex:
        log(f"<!> Error reading missing video cache: {ex}")


def save_missing_cache():
    with CACHE_LOCK:
        data = dict(config.MISSING_VIDEOS)
    try:
        temp_path = config.MISSING_CACHE_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(data, file)
        os.replace(temp_path, config.MISSING_CACHE_FILE)
    except OSError as ex:
        log(f"<!> Error saving missing video cache: {ex}")


def match_missing_video(fingerprint: str, new_path: str) -> dict | None:
    """Consume a unique missing video matching this new path."""
    if not fingerprint:
        return None
    with CACHE_LOCK:
        candidates = [
            item for item in config.MISSING_VIDEOS.values()
            if item.get("contentFingerprint") == fingerprint
        ]
        other_active = [
            item for item in config.FILES_LIST
            if item.get("contentFingerprint") == fingerprint
            and os.path.abspath(item.get("fullPath") or "") != os.path.abspath(new_path)
        ]
        if len(candidates) != 1 or other_active:
            return None
        item = candidates[0]
        config.MISSING_VIDEOS.pop(item["id"], None)
        return item


def adopt_video_identity(new_item: dict, old_item: dict) -> dict:
    """Keep old ID and user state while taking the newly discovered location."""
    result = dict(old_item)
    for field in (
        "fullPath", "path", "drive", "directory", "name", "title",
        "fileName", "ext", "fileSize", "contentFingerprint", "duration",
        "width", "height",
    ):
        if field in new_item:
            result[field] = new_item[field]
    result["id"] = old_item["id"]
    result["fileId"] = old_item["id"]
    return result


_RELINK_OVERRIDES: dict[str, str] = {}
_RELINK_IN_PROGRESS: set[str] = set()


def locate_missing_video(file_id: str) -> str | None:
    """Find a verified path without waiting for catalog repair."""
    with CACHE_LOCK:
        old_item = config.FILE_MAP.get(file_id) or config.MISSING_VIDEOS.get(file_id)
        old_item = dict(old_item) if old_item else None
        override = _RELINK_OVERRIDES.get(file_id)
    if not old_item:
        return None

    if override and os.path.isfile(override):
        return override

    old_path = old_item.get("fullPath") or old_item.get("path") or ""
    if old_path and os.path.isfile(old_path):
        return old_path
    fingerprint = old_item.get("contentFingerprint")
    filename = os.path.basename(old_path)
    size = old_item.get("fileSize")
    if not fingerprint or not filename or not size or not os.path.isdir(VOLUMES_DIR):
        return None

    matches = []
    for directory, subdirectories, filenames in os.walk(VOLUMES_DIR):
        subdirectories[:] = [
            name for name in subdirectories
            if not name.startswith(".") and name.lower() not in IGNORED_DIRS
        ]
        for name in filenames:
            if name.lower() != filename.lower():
                continue
            candidate = os.path.join(directory, name)
            try:
                if os.path.getsize(candidate) == size and video_fingerprint(candidate) == fingerprint:
                    matches.append(candidate)
            except OSError:
                continue

    if len(matches) != 1:
        log(f"--> Missing video {file_id}: found {len(matches)} matching paths.")
        return None

    new_path = matches[0]
    with CACHE_LOCK:
        _RELINK_OVERRIDES[file_id] = new_path
    return new_path


def _relink_known_video(file_id: str, new_path: str) -> str | None:
    """Save a previously verified destination under the original ID."""
    with CACHE_LOCK:
        old_item = config.FILE_MAP.get(file_id) or config.MISSING_VIDEOS.get(file_id)
        old_item = dict(old_item) if old_item else None
    if not old_item:
        return None
    old_path = old_item.get("fullPath") or old_item.get("path") or ""
    try:
        if (
            os.path.getsize(new_path) != old_item.get("fileSize")
            or video_fingerprint(new_path) != old_item.get("contentFingerprint")
        ):
            return None
    except OSError:
        return None

    relative = os.path.relpath(new_path, VOLUMES_DIR)
    parts = relative.split(os.sep)
    if len(parts) < 2:
        return None
    drive = parts[0]
    directory = "/" + "/".join(parts[1:-1]) if len(parts) > 2 else "/"
    new_item = adopt_video_identity(
        {
            "fullPath": new_path,
            "path": new_path,
            "drive": drive,
            "directory": directory,
        },
        old_item,
    )
    new_item["subfolder"] = directory
    new_key = catalog_path(new_item)

    with CACHE_LOCK:
        current = config.FILE_MAP.get(file_id)
        current_path = (current or {}).get("fullPath") or (current or {}).get("path")
        if current_path and os.path.isfile(current_path):
            _RELINK_OVERRIDES.pop(file_id, None)
            return current_path
        stale_ids = {
            item.get("id") for item in config.FILES_LIST
            if catalog_path(item) == new_key and item.get("id") != file_id
        }
        for stale_id in stale_ids:
            config.FILE_MAP.pop(stale_id, None)
        config.FILES_LIST = [
            item for item in config.FILES_LIST
            if item.get("id") != file_id and catalog_path(item) != new_key
        ]
        config.FILES_LIST.append(new_item)
        config.FILE_MAP[file_id] = new_item
        config.PATH_ID_MAP.pop(os.path.abspath(old_path).lower(), None)
        config.PATH_ID_MAP[new_key] = file_id
        config.MISSING_VIDEOS.pop(file_id, None)
        _RELINK_OVERRIDES.pop(file_id, None)

    save_disk_cache()
    save_missing_cache()
    log(f"--> Relinked missing video {file_id} to {new_path}")
    return new_path


def relink_video_async(file_id: str, new_path: str) -> None:
    with CACHE_LOCK:
        if file_id in _RELINK_IN_PROGRESS:
            return
        _RELINK_IN_PROGRESS.add(file_id)

    def repair() -> None:
        try:
            _relink_known_video(file_id, new_path)
        finally:
            with CACHE_LOCK:
                _RELINK_IN_PROGRESS.discard(file_id)

    threading.Thread(target=repair, daemon=True, name=f"Relink-{file_id[:8]}").start()


def find_and_relink_video(file_id: str) -> str | None:
    path = locate_missing_video(file_id)
    return _relink_known_video(file_id, path) if path else None

def save_disk_cache():
    try:
        with CACHE_LOCK:
            data = list(config.FILES_LIST)

        temp_file = FILE_CACHE_FILE + ".tmp"

        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
            )

        os.replace(
            temp_file,
            FILE_CACHE_FILE,
        )

        log("--> Updated SSD disk cache file.")

    except (OSError, TypeError, ValueError) as ex:
        log(f"<!> Error writing SSD disk cache: {type(ex).__name__}: {ex}")


def queue_thumbnail_for_indexed_video(
    file_id: str,
    video_path: str,
) -> None:
    """
    Queue trick-play thumbnail generation for one newly indexed video.

    Thumbnail generation runs in its own daemon thread so the filesystem
    watcher is not blocked by FFmpeg processing.

    Existing valid trick-play caches are reused by ffmpeg_service.
    """

    if not file_id:
        log("--> Thumbnail generation skipped: file ID is empty.")
        return

    if not video_path or not os.path.isfile(video_path):
        log(
            f"--> Thumbnail generation skipped: video file does not exist: {video_path}"
        )
        return

    def generate() -> None:
        try:
            trickplay_service.generate_trickplay(
                file_id,
                video_path,
            )

        except (
            OSError,
            RuntimeError,
            ValueError,
            subprocess.SubprocessError,
        ) as ex:
            log(
                f"<!> Background trick-play generation failed for "
                f"'{os.path.basename(video_path)}': "
                f"{type(ex).__name__}: {ex}"
            )

    thumbnail_thread = threading.Thread(
        target=generate,
        daemon=True,
        name=f"TrickPlay-{file_id[:8]}",
    )

    thumbnail_thread.start()

    log(f"--> Queued trick-play generation for '{os.path.basename(video_path)}'")


def try_spotlight_index_scan():
    query = " || ".join([f"kMDItemFSName == '*{ext}'" for ext in ALLOWED_EXTENSIONS])

    cmd = [
        "mdfind",
        "-onlyin",
        VOLUMES_DIR,
        query,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        paths = [p.strip() for p in result.stdout.splitlines() if p.strip()]

        new_list = []
        new_map = {}

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

            file_size = get_file_size(full_path)

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

            add_media_metadata(
                raw_item,
                full_path,
            )

            model_dict = create_video_model(raw_item).model_dump()

            new_list.append(model_dict)
            new_map[file_id] = model_dict

        if new_list:
            return new_list, new_map

    except (
        subprocess.SubprocessError,
        OSError,
        ValueError,
    ) as ex:
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

                    file_size = get_file_size(full_path)

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

                    add_media_metadata(
                        raw_item,
                        full_path,
                    )

                    model_dict = create_video_model(raw_item).model_dump()

                    results_list.append(model_dict)
                    results_map[file_id] = model_dict

    except (PermissionError, OSError) as ex:
        log(f"<!> Directory scan access warning for {current_dir}: {ex}")


def ensure_directory_indexed(
    drive_name: str,
    directory: str | None,
) -> bool:
    """
    Ensure that videos in a specific drive directory are present
    in the video catalog.

    This is a targeted filesystem scan used when an API request finds
    no indexed videos for a requested directory.

    Unlike run_catalog_scan(), this function does not rebuild the
    entire catalog. It only scans the requested physical directory
    and adds newly discovered videos to the existing catalog.

    Args:
        drive_name:
            The physical volume name, such as "Vids".

        directory:
            The directory relative to the drive root, such as
            "/Hai" or "Hai".

            An empty or None directory represents the drive root.

    Returns:
        True if one or more videos were discovered and added or
        updated.

        False if no videos were found or the directory could not
        be scanned.
    """

    if not drive_name:
        log("--> Targeted directory scan skipped: drive name is empty.")
        return False

    normalized_drive = str(drive_name).strip().strip("/")

    normalized_directory = (
        str(directory).strip().replace("\\", "/").strip("/")
        if directory is not None
        else ""
    )

    volume_path = os.path.join(
        VOLUMES_DIR,
        normalized_drive,
    )

    if normalized_directory:
        target_path = os.path.join(
            volume_path,
            *[part for part in normalized_directory.split("/") if part],
        )
    else:
        target_path = volume_path

    target_path = os.path.abspath(target_path)

    log_separator()
    log("TARGETED DIRECTORY INDEX CHECK")
    log(f"    Drive:     {normalized_drive}")
    log(f"    Directory: [{normalized_directory}]")
    log(f"    Path:      {target_path}")

    if not os.path.isdir(target_path):
        log("--> Targeted directory does not exist.")
        return False

    discovered_list = []
    discovered_map = {}

    safe_scan_directory(
        target_path,
        normalized_drive,
        volume_path,
        discovered_list,
        discovered_map,
    )

    if not discovered_list:
        log("--> Targeted directory scan found no videos.")
        return False

    added_count = 0
    updated_count = 0

    with CACHE_LOCK:
        existing_map = {
            item.get("id"): item
            for item in config.FILES_LIST
            if (
                isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and item.get("id")
            )
        }

        for file_id, model_dict in discovered_map.items():
            if not isinstance(file_id, str) or not file_id:
                log(
                    "--> Targeted scan skipped an item because it has no valid file ID."
                )
                continue

            if file_id in existing_map:
                existing_index = next(
                    (
                        index
                        for index, item in enumerate(config.FILES_LIST)
                        if (isinstance(item, dict) and item.get("id") == file_id)
                    ),
                    None,
                )

                if existing_index is not None:
                    config.FILES_LIST[existing_index] = model_dict

                config.FILE_MAP[file_id] = model_dict

                updated_count += 1

            else:
                config.FILES_LIST.append(model_dict)

                config.FILE_MAP[file_id] = model_dict

                added_count += 1

            path = model_dict.get("path") or model_dict.get("fullPath")
            if path:
                config.PATH_ID_MAP[os.path.abspath(path).lower()] = file_id

    if added_count > 0 or updated_count > 0:
        save_disk_cache()

    log(
        f"--> Targeted directory scan complete. "
        f"Found: {len(discovered_list)}, "
        f"Added: {added_count}, "
        f"Updated: {updated_count}"
    )

    return added_count > 0 or updated_count > 0


def cleanup_media_cache():
    with CACHE_LOCK:
        valid_video_ids = {
            item.get("id")
            for item in config.FILES_LIST
            if (
                isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and item.get("id")
            )
        }

    cache_directories = [
        (config.THUMB_CACHE_DIR, ".jpg"),
    ]

    for cache_directory, expected_extension in cache_directories:
        if not os.path.isdir(cache_directory):
            continue

        try:
            with os.scandir(cache_directory) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue

                    file_name = entry.name

                    if (
                        cache_directory == config.THUMB_CACHE_DIR
                        and file_name == "default_poster.jpg"
                    ):
                        continue

                    if not file_name.lower().endswith(expected_extension):
                        continue

                    file_id = os.path.splitext(file_name)[0]

                    if file_id in valid_video_ids:
                        continue

                    try:
                        os.remove(entry.path)
                        log(f"--> Removed orphaned media cache: {entry.path}")
                    except OSError as ex:
                        log(
                            f"<!> Could not remove orphaned media cache "
                            f"{entry.path}: {type(ex).__name__}: {ex}"
                        )

        except OSError as ex:
            log(
                f"<!> Media cache cleanup warning for {cache_directory}: "
                f"{type(ex).__name__}: {ex}"
            )


def run_catalog_scan():
    if config.SCAN_IN_PROGRESS:
        return

    config.SCAN_IN_PROGRESS = True
    start_time = time.time()

    with CACHE_LOCK:
        previous_items = deduplicate_catalog(config.FILES_LIST)
        for item in previous_items:
            config.PATH_ID_MAP[catalog_path(item)] = item["id"]
        missing_items = dict(config.MISSING_VIDEOS)

    try:
        new_list, new_map = try_spotlight_index_scan()

        if new_list is None or len(new_list) == 0:
            new_list = []
            new_map = {}

            if os.path.exists(VOLUMES_DIR):
                try:
                    for vol_name in os.listdir(VOLUMES_DIR):
                        if vol_name.startswith(".") or vol_name == "Macintosh HD":
                            continue

                        vol_path = os.path.join(
                            VOLUMES_DIR,
                            vol_name,
                        )

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

        # Match a disappeared video to one newly found path only when both
        # sides have a unique fingerprint. Identical copies stay separate.
        previous_by_id = {item.get("id"): item for item in previous_items}
        previously_active_paths = {
            os.path.abspath(path).lower()
            for item in previous_items
            if (path := item.get("fullPath") or item.get("path"))
            and os.path.exists(path)
        }
        valid_video_ids.update(config.MISSING_VIDEOS)
        for index, new_item in enumerate(new_list):
            old = previous_by_id.get(new_item.get("id"))
            if old:
                new_list[index] = adopt_video_identity(new_item, old)

        for old in previous_items:
            old_path = old.get("fullPath") or old.get("path") or ""
            if old_path and not os.path.exists(old_path) and old.get("contentFingerprint"):
                missing_items.setdefault(old["id"], old)

        matched_ids = set()
        for old_id, old in missing_items.items():
            fingerprint = old.get("contentFingerprint")
            if not fingerprint:
                continue
            old_matches = [
                candidate for candidate in missing_items.values()
                if candidate.get("contentFingerprint") == fingerprint
            ]
            new_matches = [
                (index, candidate) for index, candidate in enumerate(new_list)
                if candidate.get("contentFingerprint") == fingerprint
                and candidate.get("id") != old_id
                and os.path.abspath(candidate.get("fullPath") or "").lower()
                not in previously_active_paths
            ]
            if len(old_matches) == 1 and len(new_matches) == 1:
                index, candidate = new_matches[0]
                new_list[index] = adopt_video_identity(candidate, old)
                matched_ids.add(old_id)

        for old_id in matched_ids:
            missing_items.pop(old_id, None)
        new_map = {item["id"]: item for item in new_list}
        for active_id in new_map:
            missing_items.pop(active_id, None)

        with CACHE_LOCK:
            config.FILES_LIST = new_list
            config.FILE_MAP = new_map
            config.PATH_ID_MAP = catalog_path_ids(new_list)
            config.MISSING_VIDEOS = missing_items

        save_disk_cache()
        save_missing_cache()

        cleanup_media_cache()

        elapsed = round(
            time.time() - start_time,
            2,
        )

        log(
            f"--> Catalog scan complete in {elapsed}s. "
            f"Total indexed videos: {len(new_list)}"
        )

    finally:
        config.SCAN_IN_PROGRESS = False


def background_timer_loop():
    """
    Perform one complete catalog scan at server startup.

    After startup, filesystem changes are handled by watcher_service
    instead of repeatedly rescanning every video in the catalog.
    """

    run_catalog_scan()

    log(
        "--> Initial catalog scan complete. "
        "Ongoing catalog updates are handled by the file watcher."
    )

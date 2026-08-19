import os
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import config
from config import (
    ALLOWED_EXTENSIONS,
    CACHE_LOCK,
    IGNORED_DIRS,
    IGNORED_EXTENSIONS,
    VOLUMES_DIR,
    log,
)
from models.video_model import create_video_model
from services.video_service import (
    add_media_metadata,
    get_file_id,
    save_disk_cache,
)


class VideoFileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return

        # Explicitly enforce str type to resolve Pylance bytes|str union warnings
        full_path = str(event.src_path)
        path_lower = full_path.lower()

        # Validate against ignored rules and allowed extensions
        if any(part.startswith(".") for part in full_path.split("/")):
            return
        if any(ignored in path_lower for ignored in IGNORED_DIRS):
            return
        if any(ignored_ext in path_lower for ignored_ext in IGNORED_EXTENSIONS):
            return

        ext = os.path.splitext(path_lower)[1]
        if ext not in ALLOWED_EXTENSIONS:
            return

        log(f"--> New video detected by watcher: {os.path.basename(full_path)}")

        # Wait a brief moment to ensure file copy/write operations have finished settling
        time.sleep(1.5)

        if not os.path.exists(full_path):
            return

        file_id = get_file_id(full_path)
        file_name = os.path.basename(full_path)

        # Determine drive and subfolder structure
        parts = full_path.replace(VOLUMES_DIR, "").strip("/").split("/")
        if not parts:
            return

        drive_name = parts[0]
        rel_dir = os.path.dirname(full_path).split(drive_name, 1)[-1].replace("\\", "/")
        subfolder = rel_dir if not rel_dir or rel_dir.startswith("/") else "/" + rel_dir

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

        # Probe metadata exclusively for this single new file
        add_media_metadata(raw_item, full_path)

        try:
            model_dict = create_video_model(raw_item).model_dump()

            with CACHE_LOCK:
                # Avoid duplicates if already indexed
                config.FILE_MAP[file_id] = model_dict
                # Replace or append to list safely
                config.FILES_LIST = [
                    item for item in config.FILES_LIST if item.get("id") != file_id
                ]
                config.FILES_LIST.append(model_dict)

            save_disk_cache()
            log(f"--> Successfully indexed new video: {model_dict.get('title')}")
        except (OSError, RuntimeError, ValueError) as ex:
            log(
                f"<!> Failed to process watched file {file_name}: {type(ex).__name__}: {ex}"
            )


def start_file_watcher():
    if not os.path.exists(VOLUMES_DIR):
        log(f"<!> Volumes directory missing, file watcher not started: {VOLUMES_DIR}")
        return None

    event_handler = VideoFileHandler()
    observer = Observer()
    observer.schedule(event_handler, path=VOLUMES_DIR, recursive=True)
    observer.start()
    log(f"--> Real-time file watcher started on {VOLUMES_DIR}")
    return observer

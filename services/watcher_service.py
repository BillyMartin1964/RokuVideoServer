import os
import queue
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

import config
from config import (
    ALLOWED_EXTENSIONS,
    CACHE_LOCK,
    IGNORED_DIRS,
    IGNORED_EXTENSIONS,
    VOLUMES_DIR,
    log,
)
from models.directory_model import DirectoryModel, normalize_directory_path
from models.video_model import create_video_model
from services.video_service import add_media_metadata, get_file_id, save_disk_cache

# Queue for background processing of watcher events
EVENT_QUEUE: queue.Queue = queue.Queue()


def process_event_worker() -> None:
    """Worker thread that processes watched files asynchronously without blocking watchdog or API routes."""
    while True:
        try:
            event_type, full_path = EVENT_QUEUE.get()

            # Brief delay to allow file copy/write operations to finish settling
            time.sleep(1.5)

            if not os.path.exists(full_path):
                EVENT_QUEUE.task_done()
                continue

            if event_type == "directory":
                rel_path = full_path.replace(VOLUMES_DIR, "").strip("/")
                parts = [p for p in rel_path.split("/") if p]
                if parts:
                    drive_name = parts[0]
                    dir_path = "/" + "/".join(parts[1:]) if len(parts) > 1 else ""
                    norm_dir = normalize_directory_path(dir_path)
                    dir_model = DirectoryModel.create(
                        drive=drive_name, directory=norm_dir
                    )

                    # Lock strictly for in-memory write
                    with CACHE_LOCK:
                        config.DIRECTORIES_MAP[dir_model.dirKey] = (
                            dir_model.model_dump()
                        )

                    log(f"--> New directory indexed: {dir_model.dirKey}")

            elif event_type == "file":
                file_id = get_file_id(full_path)
                file_name = os.path.basename(full_path)
                parts = full_path.replace(VOLUMES_DIR, "").strip("/").split("/")
                if not parts:
                    EVENT_QUEUE.task_done()
                    continue

                drive_name = parts[0]
                raw_rel_dir = (
                    os.path.dirname(full_path)
                    .split(drive_name, 1)[-1]
                    .replace("\\", "/")
                )
                directory = normalize_directory_path(raw_rel_dir)

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
                    "directory": directory,
                    "fullPath": full_path,
                    "path": full_path,
                    "size": file_size,
                }

                # Heavy FFprobe metadata extraction happens OUTSIDE CACHE_LOCK
                add_media_metadata(raw_item, full_path)
                model_dict = create_video_model(raw_item).model_dump()

                # Fast in-memory atomic cache update
                with CACHE_LOCK:
                    config.FILE_MAP[file_id] = model_dict
                    config.FILES_LIST = [
                        item for item in config.FILES_LIST if item.get("id") != file_id
                    ]
                    config.FILES_LIST.append(model_dict)

                    dir_model = DirectoryModel.create(
                        drive=drive_name, directory=directory
                    )
                    config.DIRECTORIES_MAP[dir_model.dirKey] = dir_model.model_dump()

                # Save disk cache asynchronously after releasing CACHE_LOCK
                save_disk_cache()
                log(f"--> Indexed new video: {model_dict.get('title')}")

        except (OSError, RuntimeError, ValueError) as ex:
            log(f"<!> Error in watcher worker: {type(ex).__name__}: {ex}")
        finally:
            EVENT_QUEUE.task_done()


class MediaFileHandler(FileSystemEventHandler):
    """Handler that immediately delegates filesystem events to the queue without locking."""

    def on_created(self, event) -> None:
        full_path = str(event.src_path)
        path_lower = full_path.lower()

        if any(part.startswith(".") for part in full_path.split("/")) or any(
            ignored in path_lower for ignored in IGNORED_DIRS
        ):
            return

        if event.is_directory:
            EVENT_QUEUE.put(("directory", full_path))
            return

        if any(ignored_ext in path_lower for ignored_ext in IGNORED_EXTENSIONS):
            return

        ext = os.path.splitext(path_lower)[1]
        if ext in ALLOWED_EXTENSIONS:
            EVENT_QUEUE.put(("file", full_path))


def start_file_watcher() -> BaseObserver | None:
    if not os.path.exists(VOLUMES_DIR):
        log(f"<!> Volumes directory missing: {VOLUMES_DIR}")
        return None

    # Start background processing thread
    worker_thread = threading.Thread(target=process_event_worker, daemon=True)
    worker_thread.start()

    event_handler = MediaFileHandler()
    observer = Observer()
    observer.schedule(event_handler, path=VOLUMES_DIR, recursive=True)
    observer.start()
    log(f"--> Non-blocking file watcher started on {VOLUMES_DIR}")
    return observer

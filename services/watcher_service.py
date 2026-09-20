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
from services.video_service import (
    add_media_metadata,
    adopt_video_identity,
    catalog_path,
    get_file_id,
    match_missing_video,
    queue_thumbnail_for_indexed_video,
    run_catalog_scan,
    save_disk_cache,
    save_missing_cache,
    video_fingerprint,
)

# Queue for background processing of watcher events
EVENT_QUEUE: queue.Queue = queue.Queue()


def process_event_worker() -> None:
    """Worker thread that processes watched files asynchronously."""

    while True:
        try:
            event_type, full_path = EVENT_QUEUE.get()

            # Brief delay to allow file copy/write operations to finish settling
            time.sleep(1.5)

            if event_type == "rescan":
                run_catalog_scan()
                continue

            if event_type == "deleted":
                # A move between volumes can appear as a copy followed by a
                # delete. Drop the old path once it has actually disappeared.
                if not os.path.exists(full_path):
                    file_id = get_file_id(full_path)
                    with CACHE_LOCK:
                        removed = config.FILE_MAP.pop(file_id, None)
                        if removed is not None:
                            config.PATH_ID_MAP.pop(os.path.abspath(full_path).lower(), None)
                            if removed.get("contentFingerprint"):
                                config.MISSING_VIDEOS[file_id] = removed
                            config.FILES_LIST = [
                                item for item in config.FILES_LIST
                                if item.get("id") != file_id
                            ]
                    if removed is not None:
                        # A cross-drive copy may already have been indexed.
                        fingerprint = removed.get("contentFingerprint")
                        if fingerprint:
                            with CACHE_LOCK:
                                active_items = list(config.FILES_LIST)
                            candidates = []
                            for item in active_items:
                                path = item.get("fullPath") or ""
                                try:
                                    if (
                                        path
                                        and os.path.getsize(path) == removed.get("fileSize")
                                        and video_fingerprint(path) == fingerprint
                                    ):
                                        candidates.append(item)
                                except OSError:
                                    continue
                            if len(candidates) == 1:
                                candidate = candidates[0]
                                candidate_path = candidate.get("fullPath") or ""
                                if candidate_path and os.path.isfile(candidate_path):
                                    with CACHE_LOCK:
                                        candidate["contentFingerprint"] = fingerprint
                                    old = match_missing_video(fingerprint, candidate_path)
                                    if old:
                                        restored = adopt_video_identity(candidate, old)
                                        with CACHE_LOCK:
                                            config.FILE_MAP.pop(candidate["id"], None)
                                            config.FILE_MAP[old["id"]] = restored
                                            config.FILES_LIST = [
                                                restored if item.get("id") == candidate["id"] else item
                                                for item in config.FILES_LIST
                                            ]
                                            config.PATH_ID_MAP[os.path.abspath(candidate_path).lower()] = old["id"]
                        save_missing_cache()
                        save_disk_cache()
                continue

            if not os.path.exists(full_path):
                continue

            if event_type == "directory":
                rel_path = full_path.replace(VOLUMES_DIR, "").strip("/")

                parts = [
                    p
                    for p in rel_path.split("/")
                    if p
                ]

                if parts:
                    drive_name = parts[0]

                    dir_path = (
                        "/"
                        + "/".join(parts[1:])
                        if len(parts) > 1
                        else ""
                    )

                    norm_dir = normalize_directory_path(dir_path)

                    dir_model = DirectoryModel.create(
                        drive=drive_name,
                        directory=norm_dir,
                    )

                    # Lock strictly for in-memory write
                    with CACHE_LOCK:
                        config.DIRECTORIES_MAP[
                            dir_model.dirKey
                        ] = dir_model.model_dump()

                    log(
                        f"--> New directory indexed: "
                        f"{dir_model.dirKey}"
                    )

            elif event_type == "file":
                file_id = get_file_id(full_path)

                file_name = os.path.basename(full_path)

                parts = (
                    full_path.replace(
                        VOLUMES_DIR,
                        "",
                    )
                    .strip("/")
                    .split("/")
                )

                if not parts:
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
                    "name": os.path.splitext(
                        file_name
                    )[0],
                    "title": os.path.splitext(
                        file_name
                    )[0],
                    "drive": drive_name,
                    "directory": directory,
                    "fullPath": full_path,
                    "path": full_path,
                    "size": file_size,
                }

                # Heavy FFprobe metadata extraction happens OUTSIDE CACHE_LOCK
                add_media_metadata(
                    raw_item,
                    full_path,
                )

                model_dict = create_video_model(
                    raw_item
                ).model_dump()

                # Metadata extraction can outlive a move or deletion.
                if not os.path.exists(full_path):
                    continue

                old_item = match_missing_video(
                    model_dict.get("contentFingerprint", ""), full_path
                )
                if old_item:
                    provisional_id = file_id
                    model_dict = adopt_video_identity(model_dict, old_item)
                    file_id = old_item["id"]
                else:
                    provisional_id = file_id

                # Fast in-memory atomic cache update
                with CACHE_LOCK:
                    path_key = os.path.abspath(full_path).lower()
                    canonical_id = (
                        file_id if old_item else config.PATH_ID_MAP.get(path_key, file_id)
                    )
                    if canonical_id != file_id:
                        file_id = canonical_id
                        model_dict["id"] = canonical_id
                        model_dict["fileId"] = canonical_id
                    existing = config.FILE_MAP.get(file_id)
                    if existing:
                        model_dict = adopt_video_identity(model_dict, existing)
                        old_path = existing.get("fullPath") or ""
                        if old_path and old_path != full_path:
                            config.PATH_ID_MAP.pop(os.path.abspath(old_path).lower(), None)
                    stale_ids = {
                        item.get("id") for item in config.FILES_LIST
                        if catalog_path(item) == path_key and item.get("id") != file_id
                    }
                    stale_ids.add(provisional_id)
                    stale_ids.discard(file_id)
                    for stale_id in stale_ids:
                        config.FILE_MAP.pop(stale_id, None)
                    config.FILE_MAP[file_id] = model_dict
                    config.PATH_ID_MAP[path_key] = file_id

                    config.FILES_LIST = [
                        item
                        for item in config.FILES_LIST
                        if item.get("id") not in stale_ids
                        and item.get("id") != file_id
                        and catalog_path(item) != path_key
                    ]

                    config.FILES_LIST.append(model_dict)

                    dir_model = DirectoryModel.create(
                        drive=drive_name,
                        directory=directory,
                    )

                    config.DIRECTORIES_MAP[
                        dir_model.dirKey
                    ] = dir_model.model_dump()

                # Save disk cache after releasing CACHE_LOCK
                save_disk_cache()
                if old_item:
                    save_missing_cache()

                log(
                    f"--> Indexed new video: "
                    f"{model_dict.get('title')}"
                )

                # Queue only this newly indexed video's thumbnail.
                queue_thumbnail_for_indexed_video(
                    file_id,
                    full_path,
                )

        except (
            OSError,
            RuntimeError,
            ValueError,
        ) as ex:
            log(
                f"<!> Error in watcher worker: "
                f"{type(ex).__name__}: {ex}"
            )

        finally:
            EVENT_QUEUE.task_done()


class MediaFileHandler(FileSystemEventHandler):
    """Handler that delegates filesystem events to the queue."""

    def on_created(self, event) -> None:
        full_path = str(event.src_path)

        path_lower = full_path.lower()

        if (
            any(
                part.startswith(".")
                for part in full_path.split("/")
            )
            or any(
                ignored in path_lower
                for ignored in IGNORED_DIRS
            )
        ):
            return

        if event.is_directory:
            EVENT_QUEUE.put(
                (
                    "directory",
                    full_path,
                )
            )

            return

        if any(
            ignored_ext in path_lower
            for ignored_ext in IGNORED_EXTENSIONS
        ):
            return

        ext = os.path.splitext(path_lower)[1]

        if ext in ALLOWED_EXTENSIONS:
            EVENT_QUEUE.put(
                (
                    "file",
                    full_path,
                )
            )

    def on_moved(self, event) -> None:
        """Treat moved files/directories as created events for indexing.

        Many platforms emit a move/rename event when files are relocated
        between mounts or drives; the watcher previously handled only
        'created' events so moved-in files could be missed. Treat the
        destination path as a created event so moved files are indexed.
        """
        # Prefer destination path for moved events
        try:
            dest_path = str(event.dest_path)
        except Exception:
            return

        if not event.is_directory:
            source_key = os.path.abspath(str(event.src_path)).lower()
            destination_key = os.path.abspath(dest_path).lower()
            with CACHE_LOCK:
                old_id = config.PATH_ID_MAP.pop(source_key, None)
                if old_id:
                    config.PATH_ID_MAP[destination_key] = old_id
            if not old_id:
                self.on_deleted(event)

        # Reuse the same filtering logic as on_created
        path_lower = dest_path.lower()

        if (
            any(
                part.startswith(".")
                for part in dest_path.split("/")
            )
            or any(
                ignored in path_lower
                for ignored in IGNORED_DIRS
            )
        ):
            return

        if event.is_directory:
            EVENT_QUEUE.put(("rescan", dest_path))
            return

        if any(ignored_ext in path_lower for ignored_ext in IGNORED_EXTENSIONS):
            return

        ext = os.path.splitext(path_lower)[1]

        if ext in ALLOWED_EXTENSIONS:
            EVENT_QUEUE.put(("file", dest_path))

    def on_deleted(self, event) -> None:
        if event.is_directory:
            EVENT_QUEUE.put(("rescan", str(event.src_path)))
            return
        full_path = str(event.src_path)
        if os.path.splitext(full_path.lower())[1] in ALLOWED_EXTENSIONS:
            EVENT_QUEUE.put(("deleted", full_path))

    def on_modified(self, event) -> None:
        """Also treat some modifications as potential new files.

        Some copy/move operations are observed as a sequence of modified
        events. Handle modified events for allowed extensions so late
        writes can still be indexed.
        """
        full_path = str(event.src_path)

        path_lower = full_path.lower()

        if (
            any(part.startswith(".") for part in full_path.split("/"))
            or any(ignored in path_lower for ignored in IGNORED_DIRS)
        ):
            return

        if any(ignored_ext in path_lower for ignored_ext in IGNORED_EXTENSIONS):
            return

        ext = os.path.splitext(path_lower)[1]

        if ext in ALLOWED_EXTENSIONS:
            EVENT_QUEUE.put(("file", full_path))


def start_file_watcher() -> BaseObserver | None:
    """Start the filesystem watcher."""

    if not os.path.exists(VOLUMES_DIR):
        log(
            f"<!> Volumes directory missing: "
            f"{VOLUMES_DIR}"
        )

        return None

    worker_thread = threading.Thread(
        target=process_event_worker,
        daemon=True,
        name="WatcherEventWorker",
    )

    worker_thread.start()

    event_handler = MediaFileHandler()

    observer = Observer()

    observer.schedule(
        event_handler,
        path=VOLUMES_DIR,
        recursive=True,
    )

    observer.start()

    log(
        f"--> Non-blocking file watcher started on "
        f"{VOLUMES_DIR}"
    )

    return observer

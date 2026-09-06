#!/usr/bin/env python3

"""Roku Media Hub FastAPI server."""

import os
import queue
import socket
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import api.directories as api_directories
import api.drives as api_drives
import api.health as api_health
import api.video_models as api_video_models
import api.videos as api_videos
import config
from config import CACHE_LOCK, PORT, log, log_separator
from services import (
    ffmpeg_service,
    trickplay_service,
    video_model_service,
    video_service,
    watcher_service,
)

# ============================================================================
# Client Tracker Memory Store
# ============================================================================

CLIENT_ACTIVITY: dict[str, float] = {}
CLIENT_LOCK = threading.Lock()
START_TIME = time.time()


# ============================================================================
# Background Thumbnail Generation
# ============================================================================

THUMBNAIL_QUEUE: queue.Queue[str] = queue.Queue()
THUMBNAIL_QUEUE_LOCK = threading.Lock()
THUMBNAIL_QUEUED: set[str] = set()
THUMBNAIL_WORKER_STOP = threading.Event()
THUMBNAIL_WORKER_THREAD: threading.Thread | None = None


def queue_thumbnail_generation(file_id: str, file_path: str) -> bool:
    """Queue a video thumbnail for background generation.

    The same file cannot be queued more than once at a time.

    Returns:
        True if a new job was queued.
        False if the job was already queued or the input is invalid.
    """

    if not file_id or not file_path:
        return False

    if not os.path.isfile(file_path):
        return False

    try:
        thumbnail_path = video_model_service.thumbnail_cache_path(file_path)
    except (OSError, ValueError, TypeError):
        return False

    try:
        if os.path.exists(thumbnail_path) and os.path.getsize(thumbnail_path) > 0:
            return False
    except OSError:
        return False

    with THUMBNAIL_QUEUE_LOCK:
        if file_id in THUMBNAIL_QUEUED:
            return False

        THUMBNAIL_QUEUED.add(file_id)

    THUMBNAIL_QUEUE.put(file_id)

    return True


def thumbnail_worker() -> None:
    """Process thumbnail generation jobs in the background.

    Only one worker is used intentionally.
    """

    log("--> Background thumbnail worker started.")

    while not THUMBNAIL_WORKER_STOP.is_set():
        try:
            file_id = THUMBNAIL_QUEUE.get(timeout=1.0)

        except queue.Empty:
            continue

        try:
            with CACHE_LOCK:
                item = config.FILE_MAP.get(file_id)

                if isinstance(item, dict):
                    file_path = item.get("path") or item.get("fullPath") or ""
                else:
                    file_path = ""

            if not file_path:
                continue

            if not os.path.isfile(file_path):
                continue

            try:
                thumbnail_path = video_model_service.thumbnail_cache_path(file_path)

                if (
                    os.path.exists(thumbnail_path)
                    and os.path.getsize(thumbnail_path) > 0
                ):
                    continue

            except (
                OSError,
                ValueError,
                TypeError,
            ):
                continue

            log(f"--> Background thumbnail generation: {os.path.basename(file_path)}")

            try:
                generated_path = video_model_service.generate_thumbnail(file_path)

                if generated_path:
                    log(
                        f"--> Background thumbnail complete: "
                        f"{os.path.basename(file_path)}"
                    )
                else:
                    log(
                        f"<!> Background thumbnail unavailable: "
                        f"{os.path.basename(file_path)}"
                    )

            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                subprocess.SubprocessError,
            ) as ex:
                log(
                    f"<!> Background thumbnail generation failed for "
                    f"{os.path.basename(file_path)}: "
                    f"{type(ex).__name__}: {ex}"
                )

        finally:
            with THUMBNAIL_QUEUE_LOCK:
                THUMBNAIL_QUEUED.discard(file_id)

            THUMBNAIL_QUEUE.task_done()

    log("--> Background thumbnail worker stopped.")


def queue_missing_thumbnails() -> int:
    """Find indexed videos without cached thumbnails and queue them."""

    queued_count = 0

    with CACHE_LOCK:
        catalog_items = list(config.FILES_LIST)

    for item in catalog_items:
        if not isinstance(item, dict):
            continue

        file_id = str(item.get("id") or item.get("fileId") or "").strip()

        if not file_id:
            continue

        file_path = str(item.get("path") or item.get("fullPath") or "").strip()

        if not file_path:
            continue

        if not os.path.isfile(file_path):
            continue

        if queue_thumbnail_generation(
            file_id,
            file_path,
        ):
            queued_count += 1

    if queued_count > 0:
        log(f"--> Queued {queued_count} missing thumbnails for background generation.")

    return queued_count


def start_thumbnail_worker() -> threading.Thread:
    """Start the single background thumbnail worker."""

    global THUMBNAIL_WORKER_THREAD

    THUMBNAIL_WORKER_STOP.clear()

    worker_thread = threading.Thread(
        target=thumbnail_worker,
        daemon=True,
        name="ThumbnailGenerator",
    )

    worker_thread.start()

    THUMBNAIL_WORKER_THREAD = worker_thread

    return worker_thread


def stop_thumbnail_worker() -> None:
    """Stop the thumbnail worker cleanly."""

    THUMBNAIL_WORKER_STOP.set()

    worker_thread = THUMBNAIL_WORKER_THREAD

    if worker_thread and worker_thread.is_alive():
        worker_thread.join(timeout=5)


# ============================================================================
# Trick-Play Helpers
#
# Trick-play generation is intentionally NOT performed automatically yet.
#
# We are first proving the FFmpeg 9 -> JPEG pipeline using one video at a time
# through Swagger.
#
# Once this is verified, a background trick-play worker can be added safely.
# ============================================================================


def get_video_file_path(file_id: str) -> str | None:
    """Return the filesystem path for an indexed video."""

    if not file_id:
        return None

    with CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

        if not isinstance(item, dict):
            return None

        file_path = item.get("path") or item.get("fullPath") or ""

    file_path = str(file_path).strip()

    if not file_path:
        return None

    return file_path


def get_trickplay_frame_path(
    file_id: str,
    frame_number: int,
) -> str:
    """Return the deterministic JPEG path for a trick-play frame.

    Frame numbering is zero-based:

        000000.jpg = 0 seconds
        000001.jpg = 10 seconds
        000002.jpg = 20 seconds
        etc.
    """

    if frame_number < 0:
        raise ValueError("frame_number must be zero or greater")

    cache_directory = trickplay_service.get_trickplay_cache_dir(file_id)

    filename = f"{frame_number:06d}.jpg"

    return os.path.join(
        cache_directory,
        filename,
    )


# ============================================================================
# Pydantic Request Models
# ============================================================================


class SetAuthorizedDrivesRequest(BaseModel):
    authorized_drives: list[str] = Field(
        ...,
        json_schema_extra={
            "example": [
                "/Volumes/MediaDrive",
                "/Volumes/External1",
            ]
        },
        description=("List of drive mount points authorized for API access."),
    )


class MoveVideoRequest(BaseModel):
    file_id: str = Field(
        ...,
        json_schema_extra={"example": "vid_001"},
        description="ID of the video to move",
    )

    target_directory: str = Field(
        ...,
        json_schema_extra={"example": "/media/USB1/Movies"},
        description="Destination directory path",
    )


class RenameVideoRequest(BaseModel):
    file_id: str = Field(
        ...,
        json_schema_extra={"example": "vid_001"},
        description="ID of the video to rename",
    )

    new_name: str = Field(
        ...,
        json_schema_extra={"example": "NewMovieName.mp4"},
        description="New video filename",
    )


# ============================================================================
# FastAPI Lifespan
# ============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_separator()
    log("ROKU MEDIA HUB SERVER (FastAPI)")
    log("Starting services...")
    log_separator()

    # ------------------------------------------------------------------------
    # FFmpeg
    # ------------------------------------------------------------------------

    ffmpeg_service.initialize_ffmpeg()

    # ------------------------------------------------------------------------
    # Normal thumbnail/cache initialization
    # ------------------------------------------------------------------------

    video_model_service.ensure_default_poster()

    video_service.load_disk_cache()

    # ------------------------------------------------------------------------
    # Start normal thumbnail worker.
    # ------------------------------------------------------------------------

    start_thumbnail_worker()

    # ------------------------------------------------------------------------
    # Catalog scanner
    # ------------------------------------------------------------------------

    timer_thread = threading.Thread(
        target=video_service.background_timer_loop,
        daemon=True,
        name="CatalogScanner",
    )

    timer_thread.start()

    # ------------------------------------------------------------------------
    # File watcher
    # ------------------------------------------------------------------------

    watcher_observer = watcher_service.start_file_watcher()

    # ------------------------------------------------------------------------
    # Initial missing-thumbnail discovery
    # ------------------------------------------------------------------------

    queue_missing_thumbnails()

    # ------------------------------------------------------------------------
    # Background thumbnail monitor
    # ------------------------------------------------------------------------

    def thumbnail_monitor_loop():
        log("--> Background thumbnail monitor started.")

        while not THUMBNAIL_WORKER_STOP.is_set():
            try:
                queue_missing_thumbnails()

            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
            ) as ex:
                log(f"<!> Thumbnail monitor error: {type(ex).__name__}: {ex}")

            THUMBNAIL_WORKER_STOP.wait(timeout=10)

        log("--> Background thumbnail monitor stopped.")

    thumbnail_monitor_thread = threading.Thread(
        target=thumbnail_monitor_loop,
        daemon=True,
        name="ThumbnailMonitor",
    )

    thumbnail_monitor_thread.start()

    # ------------------------------------------------------------------------
    # Server information
    # ------------------------------------------------------------------------

    local_ip = get_local_ip()

    log_separator()
    log("Roku Media Hub Server running on:")
    log(f"    http://{local_ip}:{PORT}")
    log(f"    http://127.0.0.1:{PORT}")
    log("")
    log("Interactive API Documentation (Swagger):")
    log(f"    http://{local_ip}:{PORT}/docs")
    log_separator()

    yield

    # ------------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------------

    if watcher_observer:
        watcher_observer.stop()
        watcher_observer.join()

    stop_thumbnail_worker()


# ============================================================================
# FastAPI Application
# ============================================================================


app = FastAPI(
    title="Roku Media Hub API",
    description=(
        "FastAPI server providing media indexing, directory browsing, "
        "video models, thumbnails, trick-play JPEGs, and video streaming."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


# ============================================================================
# Global Middleware
# ============================================================================


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def track_connected_clients(request: Request, call_next):
    """Track unique client IPs and their last active timestamps."""

    client_ip = request.client.host if request.client else "unknown"

    with CLIENT_LOCK:
        CLIENT_ACTIVITY[client_ip] = time.time()

    response = await call_next(request)

    return response


# ============================================================================
# HEALTH & CLIENT ENDPOINTS
# ============================================================================


@app.get(
    "/api/health",
    tags=["Health"],
)
def get_health(request: Request):
    """Return server health and status information."""

    base_response = api_health.handle_get_health(request)

    now = time.time()

    five_mins_ago = now - 300
    twenty_four_hours_ago = now - 86400

    with CLIENT_LOCK:
        active_clients = sum(
            1 for last_seen in CLIENT_ACTIVITY.values() if last_seen >= five_mins_ago
        )

        clients_24h = sum(
            1
            for last_seen in CLIENT_ACTIVITY.values()
            if last_seen >= twenty_four_hours_ago
        )

    if isinstance(base_response, dict):
        base_response["activeClients"] = active_clients
        base_response["clients24h"] = clients_24h
        base_response["start_time"] = START_TIME

        if "driveCount" not in base_response and "drive_count" not in base_response:
            base_response["driveCount"] = (
                len(config.FILE_MAP) if hasattr(config, "FILE_MAP") else 0
            )

    return base_response


@app.get(
    "/api/clients",
    tags=["Health"],
)
def get_connected_clients():
    """Return connected client analytics."""

    now = time.time()

    five_mins_ago = now - 300
    twenty_four_hours_ago = now - 86400

    with CLIENT_LOCK:
        active_ips = [
            ip
            for ip, last_seen in CLIENT_ACTIVITY.items()
            if last_seen >= five_mins_ago
        ]

        recent_24h_ips = [
            ip
            for ip, last_seen in CLIENT_ACTIVITY.items()
            if last_seen >= twenty_four_hours_ago
        ]

    return {
        "success": True,
        "activeClientsCount": len(active_ips),
        "clients24hCount": len(recent_24h_ips),
        "activeClientIps": active_ips,
        "recent24hClientIps": recent_24h_ips,
    }


# ============================================================================
# DRIVE ENDPOINTS
# ============================================================================


@app.post(
    "/api/drives",
    tags=["Hard Drives"],
)
def set_authorized_drives(
    request: Request,
    body: SetAuthorizedDrivesRequest,
):
    """Set drives that users can see."""

    return api_drives.handle_set_authorized_drives(
        request,
        body.model_dump() if hasattr(body, "model_dump") else body.dict(),
    )


@app.get(
    "/api/drives",
    tags=["Hard Drives"],
)
def get_drives(
    request: Request,
    include_all: bool = Query(
        False,
        alias="all",
        description=("Set to true to return all drives with authorization status."),
    ),
):
    """Return available physical drives and volume metadata."""

    return api_drives.handle_get_drives(
        request,
        include_all=include_all,
    )


# ============================================================================
# DIRECTORY ENDPOINTS
# ============================================================================


@app.get(
    "/api/directories",
    tags=["Directories"],
)
def get_directories(
    request: Request,
    drive: str | None = Query(
        None,
        description="Optional drive name filter.",
    ),
):
    """Return directories for one drive or all drives."""

    if drive:
        return api_directories.handle_get_directories_by_drive(
            request,
            drive,
        )

    return api_directories.handle_get_all_directories(request)


@app.get(
    "/api/directories/drive/{drive_name}",
    tags=["Directories"],
)
def get_directories_by_drive(
    request: Request,
    drive_name: str,
):
    """Return directories associated with a specific drive."""

    return api_directories.handle_get_directories_by_drive(
        request,
        drive_name,
    )


@app.get(
    "/api/directories/children",
    tags=["Directories"],
)
def get_child_directories(
    request: Request,
    drive: str,
    directory: str = Query(
        "",
        description=("Directory whose immediate child directories should be returned."),
    ),
):
    """Return only the immediate child directories."""

    return api_directories.handle_get_child_directories(
        request,
        drive,
        directory,
    )


# ============================================================================
# VIDEO MODEL ENDPOINTS
# ============================================================================


@app.get(
    "/api/video-models",
    tags=["Video Models"],
)
def get_video_models(
    request: Request,
    drive: str | None = Query(
        None,
        description="Optional drive filter.",
    ),
    directory: str | None = Query(
        None,
        description="Optional directory/subfolder filter.",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Number of videos to skip.",
    ),
    limit: int = Query(
        60,
        ge=0,
        le=500,
        description=("Maximum number of videos to return. Use 0 for all."),
    ),
):
    """Return VideoModels matching the requested filters."""

    return api_video_models.handle_get_video_models(
        request,
        drive,
        directory,
        offset,
        limit,
    )


# ============================================================================
# VIDEO MODEL SEARCH
#
# This route must appear before /api/video-models/{file_id}.
# ============================================================================


@app.get(
    "/api/video-models/search/{fileName}",
    tags=["Video Models"],
)
def search_video_models(
    request: Request,
    fileName: str,
    search_field: Annotated[
        Literal["fileName", "title"],
        Query(
            description=(
                "Field to search. "
                "fileName searches the physical filename. "
                "title searches the VideoModel title."
            ),
        ),
    ] = "fileName",
    exclude_words: Annotated[
        str | None,
        Query(
            description=(
                "Optional words to exclude from results. "
                "Separate multiple words with spaces or commas. "
                "A video is excluded when any supplied word matches "
                "the selected search field."
            ),
        ),
    ] = None,
    drives: Annotated[
        list[str] | None,
        Query(
            description=(
                "Optional list of drive names to search. "
                "Repeat the drives parameter for multiple drives, "
                "for example: drives=Vids&drives=Movies. "
                "If omitted, all drives are searched."
            ),
        ),
    ] = None,
    directory: Annotated[
        str | None,
        Query(
            description="Optional directory/subfolder filter.",
        ),
    ] = None,
    offset: Annotated[
        int,
        Query(
            ge=0,
            description="Number of matching videos to skip.",
        ),
    ] = 0,
    limit: Annotated[
        int,
        Query(
            ge=0,
            le=500,
            description=(
                "Maximum number of matching VideoModels to return. "
                "Use 0 for all matches."
            ),
        ),
    ] = 0,
):
    """Search VideoModels using flexible text matching."""

    return api_video_models.handle_search_video_models(
        request=request,
        file_name=fileName,
        search_field=search_field,
        exclude_words=exclude_words,
        drives=drives,
        directory=directory,
        offset=offset,
        limit=limit,
    )


@app.get(
    "/api/video-models/{file_id}",
    tags=["Video Models"],
)
def get_video_model(
    request: Request,
    file_id: str,
):
    """Return the complete VideoModel for one video."""

    return api_video_models.handle_get_video_model(
        request,
        file_id,
    )


@app.get(
    "/api/video-models/{file_id}/thumbnail",
    tags=["Video Models"],
)
def get_video_model_thumbnail(
    request: Request,
    file_id: str,
):
    """Return the actual JPEG thumbnail for a video."""

    return api_video_models.handle_get_thumbnail(
        request,
        file_id,
    )


# ============================================================================
# TRICK-PLAY GENERATION
# ============================================================================


@app.post(
    "/api/trickplay/generate/{file_id}",
    tags=["Trick-Play"],
)
def generate_trickplay(
    file_id: str,
):
    """Generate trick-play JPEGs for exactly one indexed video.

    This endpoint is intentionally manual while the FFmpeg 9 trick-play
    pipeline is being tested.

    It does not scan the catalog and does not queue other videos.
    """

    file_path = get_video_file_path(file_id)

    if not file_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video Not Found",
        )

    if not os.path.isfile(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video File Not Found",
        )

    try:
        cache_directory = trickplay_service.get_trickplay_cache_dir(file_id)

    except (
        OSError,
        ValueError,
        TypeError,
    ) as ex:
        log(
            f"<!> Could not determine trick-play cache directory "
            f"for {file_id}: {type(ex).__name__}: {ex}"
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not determine trick-play cache directory.",
        ) from ex

    try:
        os.makedirs(
            cache_directory,
            exist_ok=True,
        )

    except OSError as ex:
        log(
            f"<!> Could not create trick-play cache directory "
            f"for {file_id}: {type(ex).__name__}: {ex}"
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create trick-play cache directory.",
        ) from ex

    log(f"--> Manual trick-play generation requested: {os.path.basename(file_path)}")

    try:
        trickplay_service.generate_trickplay(
            file_id,
            file_path,
        )

    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        subprocess.SubprocessError,
    ) as ex:
        log(
            f"<!> Trick-play generation failed for "
            f"{os.path.basename(file_path)}: "
            f"{type(ex).__name__}: {ex}"
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(f"Trick-play generation failed: {type(ex).__name__}: {ex}"),
        ) from ex

    # ------------------------------------------------------------------------
    # Verify that actual JPEGs now exist.
    #
    # We deliberately verify the filesystem instead of relying on the return
    # value from generate_trickplay(). This makes the Swagger test useful even
    # if the service returns None or another truthy value.
    # ------------------------------------------------------------------------

    try:
        generated_files = []

        for name in os.listdir(cache_directory):
            if not name.lower().endswith(".jpg"):
                continue

            frame_path = os.path.join(
                cache_directory,
                name,
            )

            if not os.path.isfile(frame_path):
                continue

            try:
                if os.path.getsize(frame_path) <= 0:
                    continue
            except OSError:
                continue

            generated_files.append(name)

    except OSError as ex:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Trick-play cache could not be inspected.",
        ) from ex

    generated_files.sort()

    if not generated_files:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=("FFmpeg completed but no trick-play JPEGs were found."),
        )

    return {
        "success": True,
        "message": ("Trick-play JPEG generation completed successfully."),
        "fileId": file_id,
        "fileName": os.path.basename(file_path),
        "intervalSeconds": (trickplay_service.TRICKPLAY_INTERVAL_SECONDS),
        "width": trickplay_service.TRICKPLAY_WIDTH,
        "height": trickplay_service.TRICKPLAY_HEIGHT,
        "count": len(generated_files),
        "files": generated_files,
    }


# ============================================================================
# TRICK-PLAY JPEG RETRIEVAL
# ============================================================================


@app.get(
    "/api/trickplay/{file_id}/{frame_number}",
    tags=["Trick-Play"],
)
def get_trickplay_frame(
    file_id: str,
    frame_number: int,
):
    """Return one generated trick-play JPEG.

    Frame numbering is zero-based:

        000000.jpg = 0 seconds
        000001.jpg = 10 seconds
        000002.jpg = 20 seconds
        etc.
    """

    if frame_number < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Frame number must be zero or greater.",
        )

    try:
        frame_path = get_trickplay_frame_path(
            file_id,
            frame_number,
        )

    except (
        OSError,
        ValueError,
        TypeError,
    ) as ex:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid trick-play frame request: {ex}",
        ) from ex

    if not os.path.isfile(frame_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trick-play JPEG Not Found",
        )

    try:
        frame_size = os.path.getsize(frame_path)

    except OSError as ex:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trick-play JPEG Not Found",
        ) from ex

    if frame_size <= 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trick-play JPEG Not Found",
        )

    return FileResponse(
        path=frame_path,
        media_type="image/jpeg",
        filename=os.path.basename(frame_path),
        headers={
            "Cache-Control": ("public, max-age=31536000, immutable"),
            "X-TrickPlay-Frame": str(frame_number),
            "X-TrickPlay-Interval": str(trickplay_service.TRICKPLAY_INTERVAL_SECONDS),
        },
    )


# ============================================================================
# VIDEO MANAGEMENT ENDPOINTS
# ============================================================================


@app.post(
    "/api/video-models/move",
    tags=["Video Models"],
)
def move_video(
    request: Request,
    body: MoveVideoRequest,
):
    """Move a video to an existing directory."""

    payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()

    if "file_id" in payload and "fileId" not in payload:
        payload["fileId"] = payload["file_id"]

    if "target_directory" in payload and "targetDirectory" not in payload:
        payload["targetDirectory"] = payload["target_directory"]

    return api_video_models.handle_move_video(
        request,
        payload,
    )


@app.post(
    "/api/video-models/rename",
    tags=["Video Models"],
)
def rename_video(
    request: Request,
    body: RenameVideoRequest,
):
    """Rename a video."""

    payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()

    if "file_id" in payload and "fileId" not in payload:
        payload["fileId"] = payload["file_id"]

    if "new_name" in payload and "newName" not in payload:
        payload["newName"] = payload["new_name"]

    return api_video_models.handle_rename_video(
        request,
        payload,
    )


@app.delete(
    "/api/video-models/{file_id}",
    tags=["Video Models"],
)
def delete_video(
    request: Request,
    file_id: str,
):
    """Delete a video and remove it from the catalog."""

    return api_video_models.handle_delete_video(
        request,
        file_id,
    )


# ============================================================================
# VIDEO STREAMING ENDPOINTS
# ============================================================================


@app.get(
    "/api/videos/{file_id}",
    tags=["Video Streaming"],
)
@app.head(
    "/api/videos/{file_id}",
    tags=["Video Streaming"],
)
def stream_video(
    request: Request,
    file_id: str,
):
    """Stream a video using HTTP range requests."""

    with CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

        file_path = item.get("path") if item else None

        if not file_path and item:
            file_path = item.get("fullPath")

    if not item or not file_path or not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video Not Found",
        )

    send_body = request.method != "HEAD"

    return api_videos.stream_video_file(
        request,
        file_path,
        send_body=send_body,
    )


# ============================================================================
# Client / Network Helpers
# ============================================================================


def get_local_ip() -> str:
    """Determine the Mac's LAN IP address."""

    sock: socket.socket | None = None

    try:
        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )

        sock.connect(("8.8.8.8", 80))

        ip = sock.getsockname()[0]

        if ip:
            return ip

    except OSError as ex:
        log(f"<!> Could not determine local IP via socket: {ex}")

    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    try:
        hostname = socket.gethostname()

        ip = socket.gethostbyname(hostname)

        if ip and not ip.startswith("127."):
            return ip

    except OSError as ex:
        log(f"<!> Could not determine local IP via hostname: {ex}")

    return "127.0.0.1"


# ============================================================================
# Application Entry Point
# ============================================================================


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
    )

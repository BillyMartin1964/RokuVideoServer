#!/usr/bin/env python3

import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
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


def get_local_ip():
    try:
        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )

        sock.connect(("8.8.8.8", 80))

        ip = sock.getsockname()[0]

        sock.close()

        if ip:
            return ip

    except OSError as e:
        log(f"<!> Could not determine local IP via socket: {e}")

    try:
        hostname = socket.gethostname()

        ip = socket.gethostbyname(hostname)

        if ip and not ip.startswith("127."):
            return ip

    except OSError as e:
        log(f"<!> Could not determine local IP via hostname: {e}")

    return "127.0.0.1"


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
        description="List of drive mount points authorized for API access.",
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

    ffmpeg_service.initialize_ffmpeg()
    video_model_service.ensure_default_poster()
    video_service.load_disk_cache()

    timer_thread = threading.Thread(
        target=video_service.background_timer_loop,
        daemon=True,
        name="CatalogScanner",
    )

    timer_thread.start()

    watcher_observer = watcher_service.start_file_watcher()

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

    if watcher_observer:
        watcher_observer.stop()
        watcher_observer.join()


# ============================================================================
# FastAPI Application
# ============================================================================


app = FastAPI(
    title="Roku Media Hub API",
    description=(
        "FastAPI server providing media indexing, directory browsing, "
        "video models, thumbnails, and video streaming for Roku."
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
async def track_connected_clients(
    request: Request,
    call_next,
):
    """Tracks unique client IPs and their last active timestamps."""

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
    """Return server health and status information including client metrics."""

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
    """Return connected client analytics and recently seen IP addresses."""

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
        (body.model_dump() if hasattr(body, "model_dump") else body.dict()),
    )


@app.get(
    "/api/drives",
    tags=["Hard Drives"],
)
def get_drives(
    request: Request,
    include_all: Annotated[
        bool,
        Query(
            False,
            alias="all",
            description=("Set to true to return all drives with authorization status."),
        ),
    ],
):
    """
    Return available physical drives and volume metadata.

    Defaults to returning authorized drives only.
    """

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
    drive: Annotated[
        str | None,
        Query(
            None,
            description="Optional drive name filter.",
        ),
    ],
):
    """
    Return directories.

    Without a drive parameter, returns directories for all drives.

    With a drive parameter, returns directories for that drive.
    """

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
    directory: Annotated[
        str,
        Query(
            "",
            description=(
                "Directory whose immediate child directories should be returned."
            ),
        ),
    ],
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
    drives: Annotated[
        list[str] | None,
        Query(
            None,
            description=(
                "Optional list of drive names to search. "
                "Omit the parameter, or provide an empty list, "
                "to search all drives. "
                "Repeat the parameter for multiple drives."
            ),
            openapi_examples={
                "all_drives": {
                    "summary": "All drives",
                    "description": ("Omit drives entirely to search all drives."),
                    "value": [],
                },
                "one_drive": {
                    "summary": "One drive",
                    "description": "Search only one drive.",
                    "value": ["Vids"],
                },
                "multiple_drives": {
                    "summary": "Multiple drives",
                    "description": ("Search multiple selected drives."),
                    "value": ["Vids", "Movies"],
                },
            },
        ),
    ] = None,
    directory: Annotated[
        str | None,
        Query(
            None,
            description=("Optional directory/subfolder filter."),
        ),
    ] = None,
    offset: Annotated[
        int,
        Query(
            0,
            ge=0,
            description="Number of videos to skip.",
        ),
    ] = 0,
    limit: Annotated[
        int,
        Query(
            60,
            ge=0,
            le=500,
            description=("Maximum number of videos to return. Use 0 for all."),
        ),
    ] = 60,
):
    """
    Return VideoModels.

    The optional drives parameter is an array.

    Examples:

        No drives:
            Search all drives.

        drives=Vids:
            Search only Vids.

        drives=Vids&drives=Movies:
            Search Vids and Movies.
    """

    return api_video_models.handle_get_video_models(
        request,
        drives,
        directory,
        offset,
        limit,
    )


# --------------------------------------------------------------------------
# VIDEO MODEL SEARCH
#
# This route MUST appear before /api/video-models/{file_id}.
#
# FastAPI evaluates path operations in declaration order, so the
# fixed "search" segment must be registered before the generic
# single-value path parameter.
# --------------------------------------------------------------------------


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
            None,
            description=(
                "Optional words to exclude from results. "
                "Separate multiple words with spaces or commas. "
                "A video is excluded when any supplied word "
                "matches the selected search field."
            ),
        ),
    ] = None,
    drives: Annotated[
        list[str] | None,
        Query(
            None,
            description=(
                "Optional list of drive names to search. "
                "Omit the parameter to search all drives. "
                "Repeat the parameter for multiple drives."
            ),
            openapi_examples={
                "all_drives": {
                    "summary": "All drives",
                    "description": ("Omit drives entirely to search all drives."),
                    "value": [],
                },
                "one_drive": {
                    "summary": "One drive",
                    "description": "Search only one drive.",
                    "value": ["Vids"],
                },
                "multiple_drives": {
                    "summary": "Multiple drives",
                    "description": ("Search multiple selected drives."),
                    "value": ["Vids", "Movies"],
                },
            },
        ),
    ] = None,
    directory: Annotated[
        str | None,
        Query(
            None,
            description=("Optional directory/subfolder filter."),
        ),
    ] = None,
    offset: Annotated[
        int,
        Query(
            0,
            ge=0,
            description="Number of matching videos to skip.",
        ),
    ] = 0,
    limit: Annotated[
        int,
        Query(
            0,
            ge=0,
            le=500,
            description=(
                "Maximum number of matching VideoModels "
                "to return. Use 0 for all matches."
            ),
        ),
    ] = 0,
):
    """
    Search VideoModels using flexible text matching.

    search_field determines whether the search is performed against
    the filename or the VideoModel title.

    The default is fileName.

    Search is case-insensitive and supports partial terms.

    Multiple search terms are treated as independent terms.
    All search terms must match, but they may appear in any order.

    Common filename separators and punctuation are ignored.

    Searches without spaces can match words that are separated
    by spaces or punctuation in the searched value.

    The file extension is ignored for filename searches.

    exclude_words optionally removes results containing any of
    the supplied exclusion words.

    The drives parameter is optional.

    If drives is omitted, all drives are searched.

    If one drive is supplied, only that drive is searched.

    If multiple drives are supplied, all selected drives are searched.

    Examples:

        fileName=deer

            Matches:

                Deer and Bear in the Woods.mp4

        fileName=deer bear

            Matches:

                Deer and Bear in the Woods.mp4

        fileName=bear deer

            Also matches:

                Deer and Bear in the Woods.mp4

        fileName=mom son

            Matches:

                Mom and Son Playing.mp4

        fileName=MomSon

            Also matches:

                Mom and Son Playing.mp4

        exclude_words=bear

            Excludes filenames/titles containing "bear".

        drives=Vids

            Searches only Vids.

        drives=Vids&drives=Movies

            Searches Vids and Movies.

    The response contains complete VideoModel JSON objects.
    """

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
    """
    Return the complete VideoModel for one video.

    The response contains metadata and URLs for the thumbnail
    and video stream. The video itself is not returned.
    """

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
    """
    Return the actual JPEG thumbnail for a video.

    The VideoModel contains the URL to this endpoint.
    """

    return api_video_models.handle_get_thumbnail(
        request,
        file_id,
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
    """Move a video to another directory."""

    return api_video_models.handle_move_video(
        request,
        (body.model_dump() if hasattr(body, "model_dump") else body.dict()),
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

    return api_video_models.handle_rename_video(
        request,
        (body.model_dump() if hasattr(body, "model_dump") else body.dict()),
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

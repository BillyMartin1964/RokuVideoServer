#!/usr/bin/env python3

import os
import socket
import threading
from contextlib import asynccontextmanager

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


def get_local_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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


# ============================================================================
# HEALTH ENDPOINTS
# ============================================================================


@app.get("/api/health", tags=["Health"])
def get_health(request: Request):
    """Return server health and status information."""
    return api_health.handle_get_health(request)


# ============================================================================
# DRIVE ENDPOINTS
# ============================================================================


@app.get("/api/drives", tags=["Drives"])
def get_drives(request: Request):
    """Return available physical drives and volume metadata."""
    return api_drives.handle_get_drives(request)


# ============================================================================
# DIRECTORY ENDPOINTS
# ============================================================================


@app.get("/api/directories", tags=["Directories"])
def get_directories(
    request: Request,
    drive: str | None = Query(
        None,
        description="Optional drive name filter.",
    ),
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
#
# These endpoints deal with VIDEO DATA.
#
# They do NOT stream video bytes.
#
# GET /api/video-models/{file_id}
#     Returns the complete VideoModel as JSON.
#
# GET /api/video-models/{file_id}/thumbnail
#     Returns the actual thumbnail JPEG.
#
# GET /api/video-models
#     Returns VideoModels for a drive/directory.
#
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
        description="Maximum number of videos to return. Use 0 for all.",
    ),
):
    """
    Return VideoModels.

    This endpoint returns video metadata and URLs for the videos
    matching the optional drive and directory filters.

    It does not return video file bytes.
    """

    return api_video_models.handle_get_video_models(
        request,
        drive,
        directory,
        offset,
        limit,
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
#
# These operate on the VideoModel/catalog and physical video files.
#
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
        body.model_dump() if hasattr(body, "model_dump") else body.dict(),
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
        body.model_dump() if hasattr(body, "model_dump") else body.dict(),
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
#
# Streaming is deliberately separate from VideoModel data.
#
# GET  /api/videos/{file_id}
# HEAD /api/videos/{file_id}
#
# These endpoints return actual video bytes.
#
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

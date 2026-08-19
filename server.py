#!/usr/bin/env python3

import os
import socket
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import api.directories as api_directories
import api.drives as api_drives
import api.thumbnails as api_thumbnails
import api.videos as api_videos
import config
from config import CACHE_LOCK, PORT, log, log_separator
from services import ffmpeg_service, thumbnail_service, video_service


def get_local_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if ip:
            return ip
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


# Pydantic V2 Compatible Schemas
class MoveFileRequest(BaseModel):
    file_id: str = Field(
        ...,
        json_schema_extra={"example": "vid_001"},
        description="ID of the file to move",
    )
    target_directory: str = Field(
        ...,
        json_schema_extra={"example": "/media/USB1/Movies"},
        description="Destination directory path",
    )


class RenameFileRequest(BaseModel):
    file_id: str = Field(
        ...,
        json_schema_extra={"example": "vid_001"},
        description="ID of the file to rename",
    )
    new_name: str = Field(
        ...,
        json_schema_extra={"example": "NewMovieName.mp4"},
        description="New filename with extension",
    )


# Modern FastAPI Lifespan Handler (replaces @app.on_event)
@asynccontextmanager
async def lifespan(app: FastAPI):
    log_separator()
    log("ROKU MEDIA HUB SERVER (FastAPI)")
    log("Starting services...")
    log_separator()

    ffmpeg_service.initialize_ffmpeg()
    thumbnail_service.ensure_default_poster()
    video_service.load_disk_cache()

    timer_thread = threading.Thread(
        target=video_service.background_timer_loop,
        daemon=True,
        name="CatalogScanner",
    )
    timer_thread.start()

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


# Initialize FastAPI with modern lifespan context
app = FastAPI(
    title="Roku Media Hub API",
    description="FastAPI server providing media indexing, thumbnails, and video streaming for Roku.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Global CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- API Routes ---


@app.get("/api/health", tags=["Health"])
def get_health(request: Request):
    """Health check endpoint."""
    return api_videos.handle_get_health(request)


@app.get("/api/drives", tags=["Drives"])
def get_drives(request: Request):
    """Fetch USB Drives metadata."""
    return api_drives.handle_get_drives(request)


@app.get("/api/directories", tags=["Directories"])
def get_directories(
    request: Request, drive: str | None = Query(None, description="Drive name filter")
):
    """Fetch directories (all directories OR filtered by drive). Python 3.9 friendly typing."""
    if drive:
        return api_directories.handle_get_directories_by_drive(request, drive)
    return api_directories.handle_get_all_directories(request)


@app.get("/api/directories/drive/{drive_name}", tags=["Directories"])
def get_directories_by_drive(request: Request, drive_name: str):
    """Explicit drive directory route."""
    return api_directories.handle_get_directories_by_drive(request, drive_name)


@app.get("/api/files", tags=["Files"])
def get_files(request: Request):
    """Fetch catalog video files with pagination/search query parameters."""
    start_time = time.time()
    query_params = dict(request.query_params)
    return api_videos.handle_get_files(request, query_params, start_time)


@app.get("/api/thumbnails/{file_id}", tags=["Thumbnails"])
def get_thumbnail(request: Request, file_id: str):
    """Serve cached or generated thumbnail image for a video."""
    return api_thumbnails.handle_get_thumbnail(request, file_id)


@app.get("/api/files/{file_id}", tags=["Streaming"])
@app.head("/api/files/{file_id}", tags=["Streaming"])
def stream_file(request: Request, file_id: str):
    """Stream video content or return headers for media range requests."""
    with CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)
        file_path = item.get("path") if item else None

    if not item or not file_path or not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File Not Found"
        )

    send_body = request.method != "HEAD"
    return api_videos.stream_video_file(request, file_path, send_body=send_body)


@app.post("/api/files/move", tags=["File Management"])
def move_file(request: Request, body: MoveFileRequest):
    """Move a video file to a target directory."""
    return api_videos.handle_move_file(
        request, body.model_dump() if hasattr(body, "model_dump") else body.dict()
    )


@app.post("/api/files/rename", tags=["File Management"])
def rename_file(request: Request, body: RenameFileRequest):
    """Rename a video file."""
    return api_videos.handle_rename_file(
        request, body.model_dump() if hasattr(body, "model_dump") else body.dict()
    )


@app.delete("/api/files/{file_id}", tags=["File Management"])
def delete_file(request: Request, file_id: str):
    """Delete a video file from storage."""
    return api_videos.handle_delete_file(request, file_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=PORT, reload=False)

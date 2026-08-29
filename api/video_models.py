import os
from typing import Literal

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse

import config
from services import (
    video_model_service,
    video_service,
)


def _convert_to_model(item: dict, request: Request):
    """Safely invokes the available video model conversion function."""
    converter = getattr(
        video_model_service,
        "to_video_model",
        getattr(video_model_service, "build_video_model", None),
    )
    if callable(converter):
        return converter(item, request)
    return item


def _rescan_catalog():
    """Triggers a drive rescan across available video service entry points."""
    scanner = getattr(
        video_service,
        "scan_all_drives",
        getattr(
            video_service,
            "rescan_drives",
            getattr(video_service, "scan_drives", None),
        ),
    )
    if callable(scanner):
        scanner()


def handle_get_video_models(
    request: Request,
    drive: str | None = None,
    directory: str | None = None,
    offset: int = 0,
    limit: int = 60,
):
    """Return paginated list of VideoModel objects matching optional drive/directory filters."""
    with config.CACHE_LOCK:
        videos = list(config.FILE_MAP.values())

    if drive:
        videos = [
            v
            for v in videos
            if v.get("driveName", "").lower() == drive.lower()
            or v.get("drive", "").lower() == drive.lower()
        ]

    if directory:
        norm_dir = os.path.normpath(directory).lower()
        videos = [
            v
            for v in videos
            if os.path.normpath(v.get("directory", "")).lower() == norm_dir
            or os.path.normpath(v.get("relativePath", "")).lower().startswith(norm_dir)
        ]

    total_count = len(videos)

    if limit > 0:
        paged_videos = videos[offset : offset + limit]
    else:
        paged_videos = videos[offset:]

    models = [_convert_to_model(v, request) for v in paged_videos]

    return {
        "success": True,
        "total": total_count,
        "offset": offset,
        "limit": limit,
        "count": len(models),
        "videoModels": models,
    }


def handle_search_video_models(
    request: Request,
    file_name: str,
    search_field: Literal["fileName", "title"] = "fileName",
    exclude_words: str | None = None,
    drive: str | None = None,
    directory: str | None = None,
    offset: int = 0,
    limit: int = 0,
):
    """Search VideoModels using flexible text matching and optional exclusion filters."""
    with config.CACHE_LOCK:
        videos = list(config.FILE_MAP.values())

    query_terms = file_name.lower().split()
    exclude_terms = (
        [w.lower() for w in exclude_words.replace(",", " ").split()]
        if exclude_words
        else []
    )

    matching_videos = []

    for item in videos:
        # Drive filter
        if drive and (
            item.get("driveName", "").lower() != drive.lower()
            and item.get("drive", "").lower() != drive.lower()
        ):
            continue

        # Directory filter
        if directory:
            norm_dir = os.path.normpath(directory).lower()
            item_dir = os.path.normpath(item.get("directory", "")).lower()
            item_rel = os.path.normpath(item.get("relativePath", "")).lower()
            if item_dir != norm_dir and not item_rel.startswith(norm_dir):
                continue

        # Target field string
        if search_field == "title":
            target_text = item.get("title", "").lower()
        else:
            target_text = item.get("fileName", item.get("name", "")).lower()

        # Check exclusion words
        if any(ex in target_text for ex in exclude_terms):
            continue

        # Check search terms (all terms must match)
        if all(term in target_text for term in query_terms):
            matching_videos.append(item)

    total_count = len(matching_videos)

    if limit > 0:
        paged_results = matching_videos[offset : offset + limit]
    else:
        paged_results = matching_videos[offset:]

    models = [_convert_to_model(v, request) for v in paged_results]

    return {
        "success": True,
        "total": total_count,
        "offset": offset,
        "limit": limit,
        "count": len(models),
        "videoModels": models,
    }


def handle_get_video_model(request: Request, file_id: str):
    """Return the complete VideoModel for a single video by file_id."""
    with config.CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video Model Not Found",
        )

    return _convert_to_model(item, request)


def handle_get_thumbnail(request: Request, file_id: str):
    """Return the physical JPEG thumbnail file for a video."""
    with config.CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video Not Found",
        )

    thumb_path = item.get("thumbnailPath")
    default_poster = getattr(config, "DEFAULT_POSTER_PATH", None)

    if not thumb_path or not os.path.exists(thumb_path):
        thumb_path = default_poster

    if not thumb_path or not os.path.exists(thumb_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thumbnail Image Not Found",
        )

    return FileResponse(
        path=thumb_path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def handle_move_video(request: Request, body: dict):
    """Move a video file to a new target directory and update catalog references."""
    file_id = body.get("file_id")
    target_directory = body.get("target_directory")

    if not file_id or not target_directory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="file_id and target_directory are required",
        )

    with config.CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video Not Found",
        )

    current_path = item.get("path") or item.get("fullPath")
    if not current_path or not os.path.exists(current_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Physical video file missing on disk",
        )

    file_name = os.path.basename(current_path)
    new_path = os.path.join(target_directory, file_name)

    try:
        os.makedirs(target_directory, exist_ok=True)
        os.rename(current_path, new_path)
    except OSError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to move file: {e}",
        )

    # Re-index / sync catalog state
    _rescan_catalog()

    with config.CACHE_LOCK:
        updated_item = config.FILE_MAP.get(file_id)

    model = _convert_to_model(updated_item, request) if updated_item else None

    return {
        "success": True,
        "message": "Video moved successfully",
        "videoModel": model,
    }


def handle_rename_video(request: Request, body: dict):
    """Rename a video file on disk and update catalog references."""
    file_id = body.get("file_id")
    new_name = body.get("new_name")

    if not file_id or not new_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="file_id and new_name are required",
        )

    with config.CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video Not Found",
        )

    current_path = item.get("path") or item.get("fullPath")
    if not current_path or not os.path.exists(current_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Physical video file missing on disk",
        )

    parent_dir = os.path.dirname(current_path)
    new_path = os.path.join(parent_dir, new_name)

    try:
        os.rename(current_path, new_path)
    except OSError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to rename file: {e}",
        )

    # Re-index / sync catalog state
    _rescan_catalog()

    with config.CACHE_LOCK:
        updated_item = config.FILE_MAP.get(file_id)

    model = _convert_to_model(updated_item, request) if updated_item else None

    return {
        "success": True,
        "message": "Video renamed successfully",
        "videoModel": model,
    }


def handle_delete_video(request: Request, file_id: str):
    """Delete a video file from disk and remove it from catalog indexes."""
    with config.CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video Not Found",
        )

    file_path = item.get("path") or item.get("fullPath")

    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete file on disk: {e}",
            )

    with config.CACHE_LOCK:
        config.FILE_MAP.pop(file_id, None)

    save_cache = getattr(video_service, "save_disk_cache", None)
    if callable(save_cache):
        save_cache()

    return {
        "success": True,
        "message": f"Video {file_id} deleted successfully",
    }

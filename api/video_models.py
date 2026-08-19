import os
import shutil

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse

import config
from config import CACHE_LOCK, log
from models.video_model import create_video_model
from services.thumbnail_service import generate_thumbnail
from services.video_service import get_file_id, save_disk_cache


def _get_base_url(request: Request) -> str:
    """Return the server base URL without a trailing slash."""
    if hasattr(request, "base_url"):
        return str(request.base_url).rstrip("/")

    return ""


def _get_video_item(file_id: str):
    """Return a catalog item by video ID."""
    with CACHE_LOCK:
        return config.FILE_MAP.get(file_id)


def _get_video_path(item: dict) -> str:
    """Return the physical path stored in a video catalog item."""
    return item.get("path") or item.get("fullPath") or ""


def _normalize_drive(value) -> str:
    """
    Normalize a drive name for comparison.

    Drive names are not filesystem paths, but accepting optional
    leading/trailing slashes makes API filtering more forgiving.

    Examples:
        Vids    -> Vids
        /Vids   -> Vids
        Vids/   -> Vids
        /Vids/  -> Vids
    """
    if value is None:
        return ""

    return str(value).strip().strip("/")


def _normalize_directory(value) -> str:
    """
    Normalize a directory path for comparison.

    Leading and trailing slashes are ignored so that equivalent
    directory values compare equally.

    Examples:
        Fav     -> Fav
        /Fav    -> Fav
        Fav/    -> Fav
        /Fav/   -> Fav

    The root directory is represented internally as an empty string.
    """
    if value is None:
        return ""

    normalized = str(value).strip().replace("\\", "/")

    normalized = normalized.strip("/")

    return normalized


def _model_to_dict(item: dict, base_url: str) -> dict:
    """
    Convert a catalog item into the standard VideoModel dictionary.

    The returned model contains video metadata and URLs.
    The video bytes are never included.
    """
    model = create_video_model(
        item,
        base_url=base_url,
    )

    file_id = model.id

    if file_id:
        model.posterUrl = f"{base_url}/api/video-models/{file_id}/thumbnail"

    return model.model_dump()


def handle_get_video_models(
    request: Request,
    drive: str | None = None,
    directory: str | None = None,
    offset: int = 0,
    limit: int = 60,
):
    """
    Return VideoModels for videos matching the requested filters.

    Optional filters:
        drive:
            Physical drive name.

        directory:
            Directory or subfolder containing the videos.

        offset:
            Number of matching videos to skip.

        limit:
            Maximum number of videos to return.
            0 means return all remaining videos.

    The response contains VideoModel JSON objects.
    It does not contain video bytes.
    """

    offset = max(0, offset)
    limit = max(0, min(limit, 500))

    normalized_drive = _normalize_drive(drive)

    normalized_directory = (
        _normalize_directory(directory) if directory is not None else None
    )

    base_url = _get_base_url(request)

    with CACHE_LOCK:
        matching = []

        for item in config.FILES_LIST:
            if not isinstance(item, dict):
                continue

            if normalized_drive:
                item_drive = _normalize_drive(item.get("drive", ""))

                if item_drive != normalized_drive:
                    continue

            if normalized_directory is not None:
                item_directory = _normalize_directory(item.get("directory", ""))

                item_subfolder = _normalize_directory(item.get("subfolder", ""))

                if (
                    item_directory != normalized_directory
                    and item_subfolder != normalized_directory
                ):
                    continue

            matching.append(item)

        total_count = len(matching)

        if limit == 0:
            raw_chunk = matching[offset:]
        else:
            raw_chunk = matching[offset : offset + limit]

    formatted_chunk = [
        _model_to_dict(
            item,
            base_url,
        )
        for item in raw_chunk
    ]

    next_offset = offset + len(formatted_chunk)
    has_more = next_offset < total_count

    return JSONResponse(
        {
            "success": True,
            "total": total_count,
            "offset": offset,
            "limit": limit,
            "hasMore": has_more,
            "nextOffset": next_offset,
            "data": formatted_chunk,
        }
    )


def handle_get_video_model(
    request: Request,
    file_id: str,
):
    """
    Return the complete VideoModel for one video.

    The response contains metadata and URLs.
    The video itself is never returned by this endpoint.
    """
    item = _get_video_item(file_id)

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video Not Found",
        )

    file_path = _get_video_path(item)

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video File Not Found",
        )

    base_url = _get_base_url(request)

    model_data = _model_to_dict(
        item,
        base_url,
    )

    return {
        "success": True,
        "data": model_data,
    }


def handle_get_thumbnail(
    request: Request,
    file_id: str,
):
    """
    Serve the actual JPEG thumbnail for a video.

    The VideoModel contains only the URL to this endpoint.
    """
    item = _get_video_item(file_id)

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video Not Found",
        )

    file_path = _get_video_path(item)

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video File Not Found",
        )

    thumb_path = generate_thumbnail(file_path)

    if not thumb_path or not os.path.exists(thumb_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thumbnail Generation Failed",
        )

    return FileResponse(
        thumb_path,
        media_type="image/jpeg",
    )


def handle_move_video(
    request: Request,
    body: dict,
):
    """
    Move a video to another directory and update its catalog entry.
    """
    file_id = body.get("file_id") or body.get("id")

    target_directory = (
        body.get("target_directory")
        or body.get("targetDirectory")
        or body.get("targetFolder")
    )

    if not file_id or not target_directory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video ID and target directory are required.",
        )

    item = _get_video_item(file_id)

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video Not Found",
        )

    src_path = _get_video_path(item)

    if not src_path or not os.path.exists(src_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video File Not Found",
        )

    target_directory = str(target_directory).strip()

    if not target_directory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target directory cannot be empty.",
        )

    if not os.path.isabs(target_directory):
        parent_dir = os.path.dirname(src_path)
        target_directory = os.path.join(
            parent_dir,
            target_directory,
        )

    target_directory = os.path.abspath(target_directory)

    try:
        os.makedirs(
            target_directory,
            exist_ok=True,
        )
    except OSError as ex:
        log(f"<!> Error creating target directory: {type(ex).__name__}: {ex}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to create target directory.",
        )

    dest_path = os.path.join(
        target_directory,
        os.path.basename(src_path),
    )

    if os.path.abspath(src_path) == os.path.abspath(dest_path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video is already in the target directory.",
        )

    if os.path.exists(dest_path):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=("A video with that name already exists in the target directory."),
        )

    try:
        shutil.move(
            src_path,
            dest_path,
        )
    except (OSError, shutil.Error) as ex:
        log(f"<!> Error moving video: {type(ex).__name__}: {ex}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to move video.",
        )

    base_url = _get_base_url(request)

    new_id = get_file_id(dest_path)

    drive_name = item.get("drive", "")

    volume_prefix = f"/Volumes/{drive_name}"

    if drive_name and target_directory.startswith(volume_prefix):
        relative_directory = target_directory[len(volume_prefix) :].replace("\\", "/")

        if not relative_directory:
            relative_directory = "/"
        elif not relative_directory.startswith("/"):
            relative_directory = "/" + relative_directory
    else:
        relative_directory = item.get("subfolder") or item.get("directory") or ""

    updated_item = dict(item)

    updated_item["id"] = new_id
    updated_item["fileId"] = new_id
    updated_item["path"] = dest_path
    updated_item["fullPath"] = dest_path
    updated_item["subfolder"] = relative_directory
    updated_item["directory"] = relative_directory

    updated_model = _model_to_dict(
        updated_item,
        base_url,
    )

    with CACHE_LOCK:
        config.FILE_MAP.pop(
            file_id,
            None,
        )

        config.FILE_MAP[new_id] = updated_model

        for index, catalog_item in enumerate(config.FILES_LIST):
            if catalog_item.get("id") == file_id:
                config.FILES_LIST[index] = updated_model
                break

    save_disk_cache()

    return JSONResponse(
        {
            "success": True,
            "message": "Video moved successfully.",
            "newId": new_id,
            "data": updated_model,
        }
    )


def handle_rename_video(
    request: Request,
    body: dict,
):
    """
    Rename a video and update its catalog entry.
    """
    file_id = body.get("file_id") or body.get("id")

    new_name = (body.get("new_name") or body.get("newName") or "").strip()

    if not file_id or not new_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video ID and new name are required.",
        )

    item = _get_video_item(file_id)

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video Not Found",
        )

    src_path = _get_video_path(item)

    if not src_path or not os.path.exists(src_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video File Not Found",
        )

    if os.path.basename(new_name) != new_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New name must be a filename only.",
        )

    ext = os.path.splitext(src_path)[1]

    supplied_ext = os.path.splitext(new_name)[1]

    if supplied_ext:
        new_name_without_extension = os.path.splitext(new_name)[0]
        dest_filename = new_name
    else:
        new_name_without_extension = new_name
        dest_filename = new_name + ext

    dest_path = os.path.join(
        os.path.dirname(src_path),
        dest_filename,
    )

    if os.path.abspath(src_path) == os.path.abspath(dest_path):
        return JSONResponse(
            {
                "success": True,
                "message": "Video name is unchanged.",
                "newId": file_id,
                "data": _model_to_dict(
                    item,
                    _get_base_url(request),
                ),
            }
        )

    if os.path.exists(dest_path):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A video with that name already exists.",
        )

    try:
        os.rename(
            src_path,
            dest_path,
        )
    except OSError as ex:
        log(f"<!> Error renaming video: {type(ex).__name__}: {ex}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to rename video.",
        )

    base_url = _get_base_url(request)

    new_id = get_file_id(dest_path)

    updated_item = dict(item)

    updated_item["id"] = new_id
    updated_item["fileId"] = new_id
    updated_item["name"] = new_name_without_extension
    updated_item["title"] = new_name_without_extension
    updated_item["path"] = dest_path
    updated_item["fullPath"] = dest_path

    updated_model = _model_to_dict(
        updated_item,
        base_url,
    )

    with CACHE_LOCK:
        config.FILE_MAP.pop(
            file_id,
            None,
        )

        config.FILE_MAP[new_id] = updated_model

        for index, catalog_item in enumerate(config.FILES_LIST):
            if catalog_item.get("id") == file_id:
                config.FILES_LIST[index] = updated_model
                break

    save_disk_cache()

    return JSONResponse(
        {
            "success": True,
            "message": "Video renamed successfully.",
            "newId": new_id,
            "data": updated_model,
        }
    )


def handle_delete_video(
    request: Request,
    file_id: str,
):
    """
    Delete a video from physical storage and remove it from the catalog.
    """
    item = _get_video_item(file_id)

    if not item:
        return JSONResponse(
            {
                "success": False,
                "error": "Invalid Video ID",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    src_path = _get_video_path(item)

    if src_path and os.path.exists(src_path):
        try:
            os.remove(src_path)
        except PermissionError:
            return JSONResponse(
                {
                    "success": False,
                    "error": ("Permission denied when deleting video."),
                },
                status_code=status.HTTP_403_FORBIDDEN,
            )
        except FileNotFoundError:
            pass
        except OSError as ex:
            log(f"<!> Error deleting video: {type(ex).__name__}: {ex}")
            return JSONResponse(
                {
                    "success": False,
                    "error": (f"Unable to delete video: {type(ex).__name__}: {ex}"),
                },
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    with CACHE_LOCK:
        config.FILE_MAP.pop(
            file_id,
            None,
        )

        config.FILES_LIST = [
            catalog_item
            for catalog_item in config.FILES_LIST
            if catalog_item.get("id") != file_id
        ]

    save_disk_cache()

    return JSONResponse(
        {
            "success": True,
            "message": "Video deleted successfully.",
        }
    )

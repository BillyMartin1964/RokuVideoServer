import os
import shutil

"""Modified on 9/1/2026"""

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse

import config
from config import CACHE_LOCK, log, log_separator
from models.video_model import create_video_model
from services.video_model_service import generate_thumbnail
from services.video_service import (
    ensure_directory_indexed,
    get_file_id,
    save_disk_cache,
)


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


def _normalize_drive_list(values) -> list[str]:
    """
    Normalize a collection of drive names.

    Empty values are removed and duplicate drive names are eliminated
    while preserving their original order.

    This is used by the filename search endpoint, where the Roku
    application can select zero, one, or multiple drives.
    """
    if values is None:
        return []

    if isinstance(values, str):
        values = [values]

    normalized: list[str] = []

    for value in values:
        drive = _normalize_drive(value)

        if drive and drive not in normalized:
            normalized.append(drive)

    return normalized


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


def _get_item_file_name(item: dict) -> str:
    """
    Return the filename represented by a catalog item.

    This follows the same filename construction rules used by
    create_video_model() without constructing a complete VideoModel.

    Priority:
        1. Explicit fileName
        2. name + ext
        3. name
        4. Filename extracted from path/fullPath
    """
    if not isinstance(item, dict):
        return ""

    supplied_file_name = str(item.get("fileName") or "").strip()

    if supplied_file_name:
        return supplied_file_name

    raw_name = str(item.get("name") or "").strip()

    raw_ext = str(item.get("ext") or item.get("extension") or "").strip()

    if raw_ext and not raw_ext.startswith("."):
        raw_ext = "." + raw_ext

    if raw_name:
        return f"{raw_name}{raw_ext}" if raw_ext else raw_name

    raw_path = str(item.get("path") or item.get("fullPath") or "").strip()

    if raw_path:
        return os.path.basename(raw_path)

    return ""


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
        model.thumbnailUrl = f"{base_url}/api/video-models/{file_id}/thumbnail"

    return model.model_dump()


def _get_matching_video_items(
    drive: str | None,
    directory: str | None,
):
    """
    Return catalog items matching the requested drive and directory.

    This function is used by the normal VideoModel browsing endpoint.

    The normal browsing endpoint intentionally accepts only one drive.
    Multi-drive filtering belongs to the filename search endpoint.
    """
    normalized_drive = _normalize_drive(drive)

    normalized_directory = (
        _normalize_directory(directory) if directory is not None else None
    )

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

        return matching


def _get_matching_video_items_by_drives(
    drives: list[str] | None = None,
    directory: str | None = None,
):
    """
    Return catalog items belonging to any of the selected drives.

    This function is specifically for filename searching.

    When drives contains multiple values, a video matches when its
    catalog drive matches any selected drive.

    When drives is empty or None, all drives are eligible.

    An optional directory filter can additionally restrict the results.
    """
    normalized_drives = _normalize_drive_list(drives)

    normalized_directory = (
        _normalize_directory(directory) if directory is not None else None
    )

    with CACHE_LOCK:
        matching = []

        for item in config.FILES_LIST:
            if not isinstance(item, dict):
                continue

            if normalized_drives:
                item_drive = _normalize_drive(item.get("drive", ""))

                if item_drive not in normalized_drives:
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

        return matching


def _get_matching_video_items_by_file_name(
    file_name: str,
    drives: list[str] | None = None,
    directory: str | None = None,
):
    """
    Return catalog items whose filenames contain the supplied search text.

    Matching is case-insensitive.

    The search can be restricted to zero, one, or multiple drives.

    This function only examines the existing catalog. It does not
    perform a filesystem scan.
    """
    search_text = str(file_name or "").strip().casefold()

    if not search_text:
        return []

    candidate_items = _get_matching_video_items_by_drives(
        drives,
        directory,
    )

    matching = []

    for item in candidate_items:
        item_file_name = _get_item_file_name(item)

        if not item_file_name:
            continue

        if search_text in item_file_name.casefold():
            matching.append(item)

    return matching


def handle_get_video_models(
    request: Request,
    drive: str | None = None,
    directory: str | None = None,
    offset: int = 0,
    limit: int = 60,
):
    """
    Return VideoModels for videos matching the requested filters.

    This is the VideoGrid browsing operation.

    Optional filters:
        drive:
            One physical drive name.

        directory:
            Directory or subfolder containing the videos.

        offset:
            Number of matching videos to skip.

        limit:
            Maximum number of videos to return.
            0 means return all remaining videos.

    If a specific drive and directory are requested and the existing
    catalog contains no matching videos, a targeted filesystem scan is
    performed for that directory.

    The response contains VideoModel JSON objects.
    It does not contain video bytes.

    NOTE:
        This endpoint intentionally remains single-drive. Multi-drive
        selection is handled by the filename search endpoint.
    """
    offset = max(0, offset)
    limit = max(0, min(limit, 500))

    normalized_drive = _normalize_drive(drive)

    normalized_directory = (
        _normalize_directory(directory) if directory is not None else None
    )

    base_url = _get_base_url(request)

    matching = _get_matching_video_items(
        normalized_drive,
        normalized_directory,
    )

    # ------------------------------------------------------------
    # TARGETED DIRECTORY REINDEX
    #
    # If the catalog contains no videos for a specific drive
    # directory, physically scan that directory before returning
    # zero results.
    #
    # We intentionally require BOTH drive and directory so that a
    # normal query without filters cannot accidentally trigger a
    # large filesystem scan.
    # ------------------------------------------------------------

    if len(matching) == 0 and normalized_drive and normalized_directory is not None:
        log_separator()
        log("VIDEO MODEL REQUEST FOUND NO INDEXED VIDEOS")
        log(
            f"--> Requesting targeted reindex: "
            f"drive=[{normalized_drive}] "
            f"directory=[{normalized_directory}]"
        )

        reindexed = ensure_directory_indexed(
            normalized_drive,
            normalized_directory,
        )

        if reindexed:
            log("--> Targeted reindex discovered videos. Refreshing catalog query.")

            matching = _get_matching_video_items(
                normalized_drive,
                normalized_directory,
            )
        else:
            log("--> Targeted reindex found no videos. Returning empty result.")

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


def handle_search_video_models(
    request: Request,
    file_name: str,
    search_field: str = "fileName",
    exclude_words: str | None = None,
    drives: list[str] | None = None,
    directory: str | None = None,
    offset: int = 0,
    limit: int = 0,
):
    """
    Search VideoModels by filename or title.

    The search endpoint is separate from the normal VideoGrid
    browsing endpoint.

    The drives parameter accepts zero, one, or multiple drive names.
    When multiple drives are supplied, a video may match if it belongs
    to any selected drive.

    This allows the Roku application's drive checkboxes to determine
    which drives participate in the filename search.

    Examples:

        drives=Vids

            Search only the Vids drive.

        drives=Vids&drives=Movies

            Search both Vids and Movies.

        drives=Vids&drives=Movies&drives=Archive

            Search all three selected drives.

        No drives parameter

            Search all drives.

    The search itself is currently performed against filenames.
    """
    search_text = str(file_name or "").strip()

    if not search_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Filename search text cannot be empty.",
        )

    offset = max(0, offset)
    limit = max(0, min(limit, 500))

    normalized_drives = _normalize_drive_list(drives)

    normalized_directory = (
        _normalize_directory(directory) if directory is not None else None
    )

    base_url = _get_base_url(request)

    # ------------------------------------------------------------
    # SEARCH BY FILENAME
    #
    # Drive filtering is performed here, not in the normal
    # VideoGrid VideoModel retrieval endpoint.
    # ------------------------------------------------------------

    matching = _get_matching_video_items_by_file_name(
        search_text,
        normalized_drives,
        normalized_directory,
    )

    # ------------------------------------------------------------
    # EXCLUDE WORDS
    # ------------------------------------------------------------

    if exclude_words:
        exclusions = [
            word.strip().casefold()
            for word in exclude_words.replace(",", " ").split()
            if word.strip()
        ]

        if exclusions:
            filtered_matching = []

            for item in matching:
                item_name = _get_item_file_name(item).casefold()

                if not any(exclusion in item_name for exclusion in exclusions):
                    filtered_matching.append(item)

            matching = filtered_matching

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

    log(
        f"VIDEO MODEL FILENAME SEARCH: "
        f"search=[{search_text}] "
        f"drives=[{normalized_drives}] "
        f"directory=[{normalized_directory}] "
        f"matches=[{total_count}] "
        f"returned=[{len(formatted_chunk)}]"
    )

    return JSONResponse(
        {
            "success": True,
            "search": search_text,
            "searchField": search_field,
            "drives": normalized_drives,
            "directory": normalized_directory,
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
    Move a video to an existing directory and update its catalog entry.

    This operation NEVER creates a directory.

    The target directory must already exist. If it does not exist,
    the response identifies both the exact value supplied by the client
    and the exact filesystem path resolved by the server.
    """
    file_id = body.get("file_id") or body.get("id")

    target_directory_input = (
        body.get("target_directory")
        or body.get("targetDirectory")
        or body.get("targetFolder")
    )

    if not file_id or not target_directory_input:
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

    target_directory_input = str(target_directory_input).strip()

    if not target_directory_input:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target directory cannot be empty.",
        )

    # ------------------------------------------------------------
    # RESOLVE THE CLIENT'S TARGET DIRECTORY
    #
    # IMPORTANT:
    # We do NOT create the directory.
    #
    # The Roku application is selecting an existing directory.
    # We therefore resolve the requested value and then verify that
    # the resulting filesystem path already exists.
    # ------------------------------------------------------------

    volumes_root = getattr(config, "VOLUMES_DIR", "/Volumes")

    td = target_directory_input

    if td.startswith(volumes_root):
        resolved = td

    elif td.startswith("/"):
        # Could be "/DriveName/..." -> map to "/Volumes/DriveName/..."
        parts = td.strip("/").split("/", 1)
        drive_candidate = parts[0]
        remainder = parts[1] if len(parts) > 1 else ""

        candidate_root = os.path.join(
            volumes_root,
            drive_candidate,
        )

        if os.path.isdir(candidate_root):
            resolved = (
                os.path.join(candidate_root, remainder) if remainder else candidate_root
            )
        else:
            # Treat leading-slash paths without a matching volume as
            # relative to the current item's drive.
            current_drive = str(item.get("drive", "") or "").strip()

            if current_drive:
                resolved = os.path.join(
                    volumes_root,
                    current_drive,
                    td.lstrip("/"),
                )
            else:
                parent_dir = os.path.dirname(src_path)

                resolved = os.path.join(
                    parent_dir,
                    td.lstrip("/"),
                )

    elif not os.path.isabs(td):
        # Relative path: resolve against the source file's parent directory.
        parent_dir = os.path.dirname(src_path)

        resolved = os.path.join(
            parent_dir,
            td,
        )

    else:
        # Absolute path that doesn't start with volumes_root: keep as-is.
        resolved = td

    target_directory = os.path.abspath(resolved)

    # ------------------------------------------------------------
    # LOG EXACT TARGET RESOLUTION
    # ------------------------------------------------------------

    log_separator()
    log("VIDEO MOVE TARGET DIRECTORY VALIDATION")
    log(f"--> Video ID: [{file_id}]")
    log(f"--> Source video: [{src_path}]")
    log(f"--> Requested target: [{target_directory_input}]")
    log(f"--> Resolved target: [{target_directory}]")

    # ------------------------------------------------------------
    # TARGET DIRECTORY MUST ALREADY EXIST
    #
    # DO NOT CREATE IT.
    # ------------------------------------------------------------

    if not os.path.exists(target_directory):
        log(f"<!> Target directory does not exist: [{target_directory}]")

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Target directory does not exist. "
                f"Requested: [{target_directory_input}]. "
                f"Resolved path: [{target_directory}]."
            ),
        )

    # ------------------------------------------------------------
    # TARGET PATH EXISTS BUT IS NOT A DIRECTORY
    # ------------------------------------------------------------

    if not os.path.isdir(target_directory):
        log(f"<!> Target path exists but is not a directory: [{target_directory}]")

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Target path exists but is not a directory. "
                f"Requested: [{target_directory_input}]. "
                f"Resolved path: [{target_directory}]."
            ),
        )

    # ------------------------------------------------------------
    # DESTINATION FILE
    # ------------------------------------------------------------

    dest_path = os.path.join(
        target_directory,
        os.path.basename(src_path),
    )

    log(f"--> Destination video path: [{dest_path}]")

    if os.path.abspath(src_path) == os.path.abspath(dest_path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video is already in the target directory.",
        )

    # ------------------------------------------------------------
    # DESTINATION FILE ALREADY EXISTS
    # ------------------------------------------------------------

    if os.path.exists(dest_path):
        log(f"<!> Destination video already exists: [{dest_path}]")

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A video with that name already exists in the target directory. "
                f"Destination: [{dest_path}]."
            ),
        )

    # ------------------------------------------------------------
    # MOVE THE VIDEO
    # ------------------------------------------------------------

    try:
        shutil.move(
            src_path,
            dest_path,
        )

    except (OSError, shutil.Error) as ex:
        log(f"<!> Error moving video: {type(ex).__name__}: {ex}")

        log(f"--> Source: [{src_path}]")

        log(f"--> Destination: [{dest_path}]")

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Unable to move video. "
                f"Source: [{src_path}]. "
                f"Destination: [{dest_path}]. "
                f"Error: {type(ex).__name__}: {ex}"
            ),
        )

    # ------------------------------------------------------------
    # UPDATE VIDEO MODEL
    # ------------------------------------------------------------

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

    # ------------------------------------------------------------
    # UPDATE CATALOG
    # ------------------------------------------------------------

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

    log_separator()
    log("VIDEO MOVE COMPLETED SUCCESSFULLY")
    log(f"--> Original ID: [{file_id}]")
    log(f"--> New ID: [{new_id}]")
    log(f"--> Source: [{src_path}]")
    log(f"--> Destination: [{dest_path}]")
    log(f"--> Directory: [{target_directory}]")
    log_separator()

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
                    "error": "Permission denied when deleting video.",
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

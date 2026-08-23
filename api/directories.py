#!/usr/bin/env python3

from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

# ============================================================================
# DIRECTORY FILTERING
# ============================================================================
#
# Directory navigation is based entirely on the physical filesystem.
#
# These directories are intentionally hidden from the Roku media browser:
#
#   - Any directory beginning with "." (macOS/system/hidden directories)
#   - $RECYCLE.BIN
#   - RECYCLE.BIN
#   - System Volume Information
#   - TPLDLNA (DLNA/server support directory)
#
# Video catalog data is intentionally NOT used here.
# ============================================================================


IGNORED_DIRECTORY_NAMES = {
    "$recycle.bin",
    "recycle.bin",
    "system volume information",
    "tpldlna",
}


def _should_ignore_directory(directory_name: str) -> bool:
    """
    Determine whether a physical directory should be hidden from Roku.

    Any directory beginning with "." is considered hidden.

    Specific non-hidden system/server directories can also be excluded
    through IGNORED_DIRECTORY_NAMES.
    """

    if not directory_name:
        return True

    if directory_name.startswith("."):
        return True

    return directory_name.lower() in IGNORED_DIRECTORY_NAMES


# ============================================================================
# DIRECTORY PATH HELPERS
# ============================================================================
#
# Directory navigation is based on the physical filesystem.
#
# Video catalog data is intentionally NOT used here.
# ============================================================================


def _normalize_path(path: str) -> str:
    """
    Normalize a directory path so comparisons are consistent.

    Examples:
        "/Movies/" -> "/Movies"
        "Movies"   -> "/Movies"
        "/"        -> ""
    """

    if not isinstance(path, str):
        return ""

    normalized = path.replace("\\", "/").strip()

    if not normalized or normalized == "/":
        return ""

    normalized = normalized.strip("/")

    if not normalized:
        return ""

    return "/" + normalized


def _get_drive_path(drive_name: str) -> Path:
    """
    Return the physical filesystem path for a drive.

    Example:
        "Vids2" -> /Volumes/Vids2
    """

    return Path("/Volumes") / drive_name.strip()


def _get_directory_path(
    drive_name: str,
    directory: str,
) -> Path:
    """
    Return the physical filesystem path for a directory on a drive.

    Example:
        drive_name = "Vids2"
        directory = "/Movies"

        Returns:
            /Volumes/Vids2/Movies
    """

    drive_path = _get_drive_path(drive_name)
    normalized_directory = _normalize_path(directory)

    if not normalized_directory:
        return drive_path

    relative_directory = normalized_directory.lstrip("/")

    return drive_path / relative_directory


def _get_directory_path_parts(directory_path: str) -> list[str]:
    """
    Return the directory hierarchy as a list of path components.

    Examples:

        "/Movies"
            -> ["Movies"]

        "/Movies/Action"
            -> ["Movies", "Action"]

        "/Movies/Action/Marvel/2025"
            -> ["Movies", "Action", "Marvel", "2025"]

        ""
            -> []

    The drive name is intentionally NOT included.
    """

    normalized_directory = _normalize_path(directory_path)

    if not normalized_directory:
        return []

    relative_path = normalized_directory.lstrip("/")

    if not relative_path:
        return []

    return [part for part in relative_path.split("/") if part]


def _create_directory_item(
    drive_name: str,
    directory_path: str,
) -> dict:
    """
    Create the directory response object used by the Roku application.

    directory_path is relative to the drive and always begins with "/".

    Example:

        drive_name = "Vids2"
        directory_path = "/Movies/Action/2025"

    Produces an item representing:

        /Volumes/Vids2/Movies/Action/2025

    The complete directory hierarchy is preserved in:

        path
        depth
        parent
    """

    normalized_directory = _normalize_path(directory_path)

    title = (
        normalized_directory.rsplit("/", 1)[-1] if normalized_directory else drive_name
    )

    dir_key = f"{drive_name}|{normalized_directory}"

    if normalized_directory:
        display_name = f"{drive_name} / {normalized_directory.lstrip('/')}"
    else:
        display_name = drive_name

    path_parts = _get_directory_path_parts(normalized_directory)

    depth = len(path_parts)

    if len(path_parts) > 1:
        parent = path_parts[-2]
    else:
        parent = ""

    return {
        "drive": drive_name,
        "subfolder": normalized_directory,
        "dirKey": dir_key,
        "thumbUrl": "",
        "name": display_name,
        "title": title,
        "path": path_parts,
        "depth": depth,
        "parent": parent,
    }


# ============================================================================
# IMMEDIATE CHILD DIRECTORY DISCOVERY
# ============================================================================


def _get_immediate_child_directories(
    drive_name: str,
    parent_directory: str,
) -> list:
    """
    Return only the immediate physical child directories beneath
    the selected directory.
    """

    target_path = _get_directory_path(
        drive_name,
        parent_directory,
    )

    if not target_path.exists():
        return []

    if not target_path.is_dir():
        return []

    children = []

    try:
        for entry in target_path.iterdir():
            if not entry.is_dir():
                continue

            child_name = entry.name

            if _should_ignore_directory(child_name):
                continue

            normalized_parent = _normalize_path(parent_directory)

            if normalized_parent:
                child_path = f"{normalized_parent}/{child_name}"
            else:
                child_path = f"/{child_name}"

            children.append(
                _create_directory_item(
                    drive_name,
                    child_path,
                )
            )

    except OSError:
        return []

    return sorted(
        children,
        key=lambda item: item["title"].lower(),
    )


# ============================================================================
# GET ALL DIRECTORIES
# ============================================================================


def handle_get_all_directories(request: Request):
    """
    Return the immediate top-level directories for all mounted drives.

    Directory information comes directly from the physical filesystem.
    """

    del request

    volumes_path = Path("/Volumes")
    directory_list = []

    if not volumes_path.exists() or not volumes_path.is_dir():
        return JSONResponse(
            content={
                "success": True,
                "total": 0,
                "data": [],
            }
        )

    try:
        for drive_entry in volumes_path.iterdir():
            if not drive_entry.is_dir():
                continue

            if _should_ignore_directory(drive_entry.name):
                continue

            drive_name = drive_entry.name

            children = _get_immediate_child_directories(
                drive_name,
                "",
            )

            directory_list.extend(children)

    except OSError:
        return JSONResponse(
            content={
                "success": True,
                "total": 0,
                "data": [],
            }
        )

    return JSONResponse(
        content={
            "success": True,
            "total": len(directory_list),
            "data": directory_list,
        }
    )


# ============================================================================
# GET DIRECTORIES FOR A DRIVE
# ============================================================================


def handle_get_directories_by_drive(
    request: Request,
    drive_name: str,
):
    """
    Return only the immediate physical child directories of a drive.
    """

    del request

    drive_name = drive_name.strip()

    if not drive_name:
        return JSONResponse(
            content={
                "success": False,
                "drive": drive_name,
                "total": 0,
                "data": [],
            }
        )

    drive_path = _get_drive_path(drive_name)

    if not drive_path.exists() or not drive_path.is_dir():
        return JSONResponse(
            content={
                "success": False,
                "drive": drive_name,
                "total": 0,
                "data": [],
            }
        )

    directory_list = _get_immediate_child_directories(
        drive_name,
        "",
    )

    return JSONResponse(
        content={
            "success": True,
            "drive": drive_name,
            "total": len(directory_list),
            "data": directory_list,
        }
    )


# ============================================================================
# GET SUBDIRECTORIES FOR A DIRECTORY
# ============================================================================


def handle_get_subdirectories(
    request: Request,
    drive_name: str,
    directory: str,
):
    """
    Return only the immediate physical subdirectories of a selected
    directory.
    """

    del request

    drive_name = drive_name.strip()
    normalized_directory = _normalize_path(directory)

    if not drive_name:
        return JSONResponse(
            content={
                "success": False,
                "drive": drive_name,
                "directory": normalized_directory,
                "total": 0,
                "data": [],
            }
        )

    target_path = _get_directory_path(
        drive_name,
        normalized_directory,
    )

    if not target_path.exists() or not target_path.is_dir():
        return JSONResponse(
            content={
                "success": False,
                "drive": drive_name,
                "directory": normalized_directory,
                "total": 0,
                "data": [],
            }
        )

    children = _get_immediate_child_directories(
        drive_name,
        normalized_directory,
    )

    return JSONResponse(
        content={
            "success": True,
            "drive": drive_name,
            "directory": normalized_directory,
            "total": len(children),
            "data": children,
        }
    )


# ============================================================================
# LEGACY CHILD DIRECTORY ENDPOINT
# ============================================================================


def handle_get_child_directories(
    request: Request,
    drive_name: str,
    parent_directory: str,
):
    """
    Return only the immediate physical child directories beneath
    the selected directory. Retained as a compatibility endpoint.
    """

    return handle_get_subdirectories(
        request,
        drive_name,
        parent_directory,
    )

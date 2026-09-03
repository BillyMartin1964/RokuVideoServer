#!/usr/bin/env python3

from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

import config
from models.directory_model import DirectoryModel

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
    """Determine whether a physical directory should be hidden from Roku.

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
    """Normalize a directory path so comparisons are consistent.

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
    """Return the physical filesystem path for a drive.

    Example:
        "Vids2" -> /Volumes/Vids2
    """
    return Path(config.VOLUMES_DIR) / drive_name.strip()


def _get_directory_path(
    drive_name: str,
    directory: str,
) -> Path:
    """Return the physical filesystem path for a directory on a drive.

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
    """Return the directory hierarchy as a list of path components.

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


# ============================================================================
# CHILD DIRECTORY COUNT
# ============================================================================


def _get_child_directory_count(directory_path: Path) -> int:
    """Return the number of immediate child directories visible to Roku.

    Only directories that would be returned by the directory API are counted.
    Hidden and system/server directories excluded by _should_ignore_directory()
    are therefore not included in the count.

    The count includes only immediate children. It does not recursively count
    directories farther down the hierarchy.

    Examples:

        /Movies
            /Action
            /Comedy
            /Drama

        Returns:
            3

    If the directory cannot be read, the count safely returns 0.
    """

    if not directory_path.exists():
        return 0

    if not directory_path.is_dir():
        return 0

    child_count = 0

    try:
        for entry in directory_path.iterdir():
            if not entry.is_dir():
                continue

            if _should_ignore_directory(entry.name):
                continue

            child_count += 1

    except OSError:
        return 0

    return child_count


def _create_directory_item(
    drive_name: str,
    directory_path: str,
    child_count: int = 0,
) -> dict:
    """Create the directory response object using DirectoryModel.

    Instantiating DirectoryModel guarantees both 'directory' and 'subfolder'
    are populated alongside standard fields (isFolder, depth, parent, path,
    and childCount).
    """

    normalized_directory = _normalize_path(directory_path)

    model = DirectoryModel.create(
        drive=drive_name,
        directory=normalized_directory,
        child_count=child_count,
    )

    return model.model_dump()


# ============================================================================
# IMMEDIATE CHILD DIRECTORY DISCOVERY
# ============================================================================


def _get_immediate_child_directories(
    drive_name: str,
    parent_directory: str,
) -> list:
    """Return only the immediate physical child directories beneath the selected directory.

    Each returned directory also includes the number of its own immediate
    child directories.
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

            child_count = _get_child_directory_count(entry)

            children.append(
                _create_directory_item(
                    drive_name,
                    child_path,
                    child_count,
                )
            )

    except OSError:
        return []

    return sorted(
        children,
        key=lambda item: item.get("title", "").lower(),
    )


# ============================================================================
# GET ALL DIRECTORIES
# ============================================================================


def handle_get_all_directories(request: Request):
    """Return the immediate top-level directories for all mounted drives.

    Directory information comes directly from the physical filesystem.
    """

    del request

    volumes_path = Path(config.VOLUMES_DIR)
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
    """Return only the immediate physical child directories of a drive."""

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
    """Return only the immediate physical subdirectories of a selected directory."""

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
    """Return only the immediate physical child directories beneath the selected directory.

    Retained as a compatibility endpoint.
    """

    return handle_get_subdirectories(
        request,
        drive_name,
        parent_directory,
    )

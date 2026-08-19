from fastapi import Request
from fastapi.responses import JSONResponse

import config
from config import CACHE_LOCK


def handle_get_all_directories(request: Request):
    """Returns a list of all unique subfolders across indexed files."""
    directories = set()

    with CACHE_LOCK:
        for item in config.FILES_LIST:
            subfolder = item.get("subfolder")
            if subfolder:
                directories.add(subfolder)

    sorted_dirs = sorted(list(directories))
    return JSONResponse(
        content={
            "success": True,
            "data": sorted_dirs,
            "count": len(sorted_dirs),
        }
    )


def handle_get_directories_by_drive(request: Request, drive_name: str):
    """Returns a list of unique subfolders for a specific drive volume."""
    directories = set()

    with CACHE_LOCK:
        for item in config.FILES_LIST:
            if item.get("drive") == drive_name:
                subfolder = item.get("subfolder")
                if subfolder:
                    directories.add(subfolder)

    sorted_dirs = sorted(list(directories))
    return JSONResponse(
        content={
            "success": True,
            "drive": drive_name,
            "data": sorted_dirs,
            "count": len(sorted_dirs),
        }
    )

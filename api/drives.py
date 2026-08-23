#!/usr/bin/env python3

from fastapi import Request

import config
from services import drive_service


def handle_get_drives(request: Request | None = None) -> dict:
    """Return available physical drives and authorized mount point status."""
    return drive_service.get_drives_response()


def handle_set_authorized_drives(
    request: Request | None = None,
    data: dict | None = None,
) -> dict:
    """Set authorized drives and update persisted mount points configuration."""
    if not data:
        return {
            "success": False,
            "error": "Missing request body",
            "drives": drive_service.get_drives_response().get("drives", []),
        }

    authorized_drives = data.get("authorized_drives", [])
    if not isinstance(authorized_drives, list):
        return {
            "success": False,
            "error": "authorized_drives must be a list of paths",
            "drives": drive_service.get_drives_response().get("drives", []),
        }

    # Execute save_authorized_mountpoints dynamically if present on config
    save_fn = getattr(config, "save_authorized_mountpoints", None)
    if callable(save_fn):
        save_fn(authorized_drives)
    else:
        config.__dict__["AUTHORIZED_MOUNTPOINTS"] = authorized_drives

    return drive_service.get_drives_response()
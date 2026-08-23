#!/usr/bin/env python3

import json

from fastapi import Request
from fastapi.responses import JSONResponse

from services.drive_service import (
    get_drives_response,
    save_authorized_drives,
)


def handle_get_drives(request: Request, include_all: bool = False) -> JSONResponse:
    """
    Return drives response.

    If include_all is False (default), returns ONLY authorized drives for Roku.
    If include_all is True, returns ALL drives with 'authorized' booleans for Swift.
    """
    del request
    # only_authorized is True when include_all is False
    response_data = get_drives_response(only_authorized=not include_all)
    return JSONResponse(content=response_data)


def handle_set_authorized_drives(request: Request, body: dict) -> JSONResponse:
    """
    Set authorized drives list from a dict body (passed by server.py handler).

    Expected body payload:
      { "authorized_drives": ["Vids1", "Vids2"] }
    """
    del request
    drives = body.get("authorized_drives", [])

    if not isinstance(drives, list):
        return JSONResponse(
            content={
                "success": False,
                "error": "Invalid payload format. Expected array of drive names.",
            },
            status_code=400,
        )

    success = save_authorized_drives(drives)
    return JSONResponse(
        content={
            "success": success,
            "authorized_drives": drives if success else [],
        }
    )


async def handle_update_authorized_drives(request: Request) -> JSONResponse:
    """
    Update authorized drives list from a raw POST JSON request.
    """
    try:
        body = await request.json()
        return handle_set_authorized_drives(request, body)
    except (json.JSONDecodeError, TypeError, ValueError) as ex:
        return JSONResponse(
            content={"success": False, "error": f"Failed to process request: {ex}"},
            status_code=400,
        )

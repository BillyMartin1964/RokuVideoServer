import os

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse

import config
from config import CACHE_LOCK
from services.thumbnail_service import generate_thumbnail


def handle_get_thumbnail(request: Request, file_id: str):
    """Serves a cached thumbnail or triggers dynamic generation."""
    with CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)
        file_path = item.get("path") if item else None

    if not item or not file_path or not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File Not Found"
        )

    thumb_path = generate_thumbnail(file_path)
    if not thumb_path or not os.path.exists(thumb_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thumbnail Generation Failed",
        )

    return FileResponse(thumb_path, media_type="image/jpeg")

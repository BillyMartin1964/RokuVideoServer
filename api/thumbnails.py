import os
import shutil
import config
from config import log, log_separator, DEFAULT_POSTER_FILE, CACHE_LOCK
from services.thumbnail_service import generate_thumbnail, ensure_default_poster


def handle_get_thumbnail(handler, file_id):
    log_separator()
    log(f"--> THUMBNAIL HTTP REQUEST: id={file_id}")

    with CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)
        file_path = item.get("path", "") if item else None

    if not item or not file_path or not os.path.exists(file_path):
        log("<!> Thumbnail request failed: file path does not exist.")
    else:
        thumb_path = generate_thumbnail(file_path)
        if thumb_path and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            file_size = os.path.getsize(thumb_path)
            handler.send_response(200)
            handler.send_header("Content-Type", "image/jpeg")
            handler.send_header("Content-Length", str(file_size))
            handler.send_header("Cache-Control", "public, max-age=86400")
            handler.send_cors_headers()
            handler.end_headers()

            try:
                with open(thumb_path, "rb") as f:
                    shutil.copyfileobj(f, handler.wfile)
                log("--> Thumbnail sent successfully.")
            except BrokenPipeError:
                log("<!> Client disconnected while receiving thumbnail.")
            return

    if ensure_default_poster():
        file_size = os.path.getsize(DEFAULT_POSTER_FILE)
        handler.send_response(200)
        handler.send_header("Content-Type", "image/jpeg")
        handler.send_header("Content-Length", str(file_size))
        handler.send_header("Cache-Control", "no-cache")
        handler.send_cors_headers()
        handler.end_headers()

        try:
            with open(DEFAULT_POSTER_FILE, "rb") as f:
                shutil.copyfileobj(f, handler.wfile)
            log("--> Default poster sent.")
        except BrokenPipeError:
            log("<!> Client disconnected while receiving fallback.")
        return

    handler.send_error(404, "Thumbnail Not Found")
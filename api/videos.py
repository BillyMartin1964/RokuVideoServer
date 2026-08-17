import os
import re
import time

import config
from config import CACHE_LOCK, CHUNK_SIZE, log
from services.video_service import (
    get_file_id,
    save_disk_cache,
)


def handle_get_health(handler):
    with CACHE_LOCK:
        total_videos = len(config.FILES_LIST)

    handler.send_json_response(
        {
            "success": True,
            "server": "Roku Media Hub",
            "ffmpegFound": config.FFMPEG_PATH is not None,
            "ffmpegPath": config.FFMPEG_PATH,
            "ffprobeFound": config.FFPROBE_PATH is not None,
            "ffprobePath": config.FFPROBE_PATH,
            "thumbnailDirectory": config.THUMB_CACHE_DIR,
            "videoCount": total_videos,
            "uptimeSeconds": round(time.time() - config.SERVER_START_TIME, 1),
        }
    )


def handle_get_files(handler, query_params, start_time):
    drive_filter = query_params.get("drive", [None])[0]
    subfolder_filter = query_params.get("subfolder", [None])[0]

    try:
        offset = int(query_params.get("offset", [0])[0])
        limit = int(query_params.get("limit", [60])[0])
    except ValueError:
        handler.send_json_response(
            {"success": False, "error": "Invalid offset or limit."},
            status=400,
        )
        return

    offset = max(0, offset)

    with CACHE_LOCK:
        total_count = len(config.FILES_LIST)

        if drive_filter is not None or subfolder_filter is not None:
            matching = []
            for item in config.FILES_LIST:
                ok = True
                if drive_filter:
                    ok = ok and (item.get("drive", "") == drive_filter)
                if subfolder_filter:
                    ok = ok and (item.get("subfolder", "") == subfolder_filter)
                if ok:
                    matching.append(item)

            total_count = len(matching)
            chunk = (
                matching[offset:]
                if limit == 0
                else matching[offset : offset + min(limit, 500)]
            )
        else:
            if limit == 0:
                chunk = config.FILES_LIST[offset:]
            else:
                limit = max(1, min(limit, 500))
                chunk = config.FILES_LIST[offset : offset + limit]

        has_more = offset + len(chunk) < total_count
        next_offset = offset + len(chunk)

    elapsed = round((time.time() - start_time) * 1000, 2)
    log(
        f"--> Stream Batch: Sent items {offset} to {next_offset} / {total_count} in {elapsed}ms"
    )

    handler.send_json_response(
        {
            "success": True,
            "total": total_count,
            "offset": offset,
            "limit": limit,
            "hasMore": has_more,
            "nextOffset": next_offset,
            "data": chunk,
        }
    )


def handle_move_file(handler, body):
    file_id = body.get("id")
    target_folder = body.get("targetFolder", "Archive")

    with CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

    if not item or not item.get("path") or not os.path.exists(item["path"]):
        handler.send_error(400, "Invalid File ID or file missing")
        return

    src_path = item["path"]
    parent_dir = os.path.dirname(src_path)
    dest_dir = os.path.join(parent_dir, target_folder)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, os.path.basename(src_path))

    import shutil

    shutil.move(src_path, dest_path)

    new_id = get_file_id(dest_path)
    item["id"] = new_id
    item["path"] = dest_path

    old_sub = item.get("subfolder", "").rstrip("/")
    item["subfolder"] = f"{old_sub}/{target_folder}" if old_sub else f"/{target_folder}"

    with CACHE_LOCK:
        config.FILE_MAP.pop(file_id, None)
        config.FILE_MAP[new_id] = item
        for idx, cat_item in enumerate(config.FILES_LIST):
            if cat_item.get("id") == file_id:
                config.FILES_LIST[idx] = item
                break

    save_disk_cache()
    handler.send_json_response(
        {"success": True, "message": "File moved successfully", "newId": new_id}
    )


def handle_rename_file(handler, body):
    file_id = body.get("id")
    new_name = (body.get("newName") or "").strip()

    with CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

    if not item or not new_name or not os.path.exists(item.get("path", "")):
        handler.send_error(400, "Invalid Parameters")
        return

    src_path = item["path"]
    ext = os.path.splitext(src_path)[1]
    dest_path = os.path.join(os.path.dirname(src_path), new_name + ext)

    if os.path.exists(dest_path):
        handler.send_error(409, "A file with that name already exists")
        return

    os.rename(src_path, dest_path)
    new_id = get_file_id(dest_path)
    item["id"] = new_id
    item["name"] = new_name
    item["path"] = dest_path

    with CACHE_LOCK:
        config.FILE_MAP.pop(file_id, None)
        config.FILE_MAP[new_id] = item
        for idx, cat_item in enumerate(config.FILES_LIST):
            if cat_item.get("id") == file_id:
                config.FILES_LIST[idx] = item
                break

    save_disk_cache()
    handler.send_json_response(
        {
            "success": True,
            "message": "File renamed successfully",
            "newId": new_id,
        }
    )


def handle_delete_file(handler, file_id):
    with CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

    if item:
        src_path = item.get("path")
        if src_path and os.path.exists(src_path):
            try:
                os.remove(src_path)
            except Exception as ex:
                log(f"<!> Error deleting file: {type(ex).__name__}: {ex}")
                handler.send_error(500, "Unable to delete file")
                return

        with CACHE_LOCK:
            config.FILE_MAP.pop(file_id, None)
            config.FILES_LIST = [i for i in config.FILES_LIST if i.get("id") != file_id]

        save_disk_cache()
        handler.send_json_response(
            {"success": True, "message": "File deleted successfully"}
        )
        return

    handler.send_error(400, "Invalid File ID")


def stream_video_file(handler, file_path, send_body=True):
    if not os.path.exists(file_path):
        handler.send_error(404, "File Not Found")
        return

    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        handler.send_error(404, "Unable to determine file size")
        return

    if file_size <= 0:
        handler.send_error(404, "Empty File")
        return

    range_header = handler.headers.get("Range")

    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{file_size}")
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Connection", "keep-alive")
            handler.end_headers()
            return

        start_text, end_text = match.group(1), match.group(2)

        if start_text == "" and end_text != "":
            suffix_length = int(end_text)
            if suffix_length <= 0:
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{file_size}")
                handler.end_headers()
                return
            start = max(0, file_size - min(suffix_length, file_size))
            end = file_size - 1
        else:
            if start_text == "":
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{file_size}")
                handler.end_headers()
                return
            start = int(start_text)
            if start >= file_size:
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{file_size}")
                handler.send_header("Accept-Ranges", "bytes")
                handler.end_headers()
                return
            end = int(end_text) if end_text else file_size - 1
            end = min(end, file_size - 1)

            if end < start:
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{file_size}")
                handler.send_header("Accept-Ranges", "bytes")
                handler.end_headers()
                return

        length = end - start + 1
        content_range = f"bytes {start}-{end}/{file_size}"
        handler.send_file_headers(
            file_path,
            file_size,
            status=206,
            content_length=length,
            content_range=content_range,
        )

        if not send_body:
            return

        try:
            with open(file_path, "rb") as f:
                f.seek(start)
                bytes_remaining = length
                while bytes_remaining > 0:
                    chunk = f.read(min(CHUNK_SIZE, bytes_remaining))
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    bytes_remaining -= len(chunk)
        except BrokenPipeError:
            log(f"--> Video client disconnected during range {start}-{end}.")
        return

    handler.send_file_headers(
        file_path, file_size, status=200, content_length=file_size
    )

    if not send_body:
        return

    try:
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except BrokenPipeError:
        log("--> Video client disconnected.")

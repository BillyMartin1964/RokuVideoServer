import os
import re
import shutil
import time

from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import config
from config import CACHE_LOCK, CHUNK_SIZE, log
from models.video_model import create_video_model
from services.video_service import get_file_id, save_disk_cache


def handle_get_files(request, query_params, start_time):
    drive_filter = query_params.get("drive", [None])[0]
    subfolder_filter = query_params.get("subfolder", [None])[0]

    try:
        offset = int(query_params.get("offset", [0])[0])
        limit = int(query_params.get("limit", [60])[0])
    except ValueError:
        return JSONResponse(
            {"success": False, "error": "Invalid offset or limit."},
            status_code=400,
        )

    offset = max(0, offset)

    base_url = str(request.base_url).rstrip("/") if hasattr(request, "base_url") else ""

    with CACHE_LOCK:
        total_count = len(config.FILES_LIST)

        if drive_filter is not None or subfolder_filter is not None:
            matching = []
            for item in config.FILES_LIST:
                ok = True
                if drive_filter:
                    ok = ok and (item.get("drive", "") == drive_filter)
                if subfolder_filter:
                    ok = ok and (
                        item.get("subfolder", "") == subfolder_filter
                        or item.get("directory", "") == subfolder_filter
                    )
                if ok:
                    matching.append(item)

            total_count = len(matching)
            raw_chunk = (
                matching[offset:]
                if limit == 0
                else matching[offset : offset + min(limit, 500)]
            )
        else:
            if limit == 0:
                raw_chunk = config.FILES_LIST[offset:]
            else:
                limit = max(1, min(limit, 500))
                raw_chunk = config.FILES_LIST[offset : offset + limit]

        # Standardize every item in the chunk against VideoModel contract
        formatted_chunk = [
            create_video_model(item, base_url=base_url).model_dump()
            for item in raw_chunk
        ]

        has_more = offset + len(formatted_chunk) < total_count
        next_offset = offset + len(formatted_chunk)

    elapsed = round((time.time() - start_time) * 1000, 2)
    log(
        f"--> Stream Batch: Sent items {offset} to {next_offset} / {total_count} in {elapsed}ms"
    )

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


def handle_move_file(request, body):
    file_id = body.get("id")
    target_folder = body.get("targetFolder", "Archive")

    with CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

    if (
        not item
        or (not item.get("path") and not item.get("fullPath"))
        or not os.path.exists(item.get("path") or item.get("fullPath", ""))
    ):
        raise HTTPException(status_code=400, detail="Invalid File ID or file missing")

    src_path = item.get("path") or item.get("fullPath")
    parent_dir = os.path.dirname(src_path)
    dest_dir = os.path.join(parent_dir, target_folder)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, os.path.basename(src_path))

    shutil.move(src_path, dest_path)

    base_url = str(request.base_url).rstrip("/") if hasattr(request, "base_url") else ""
    new_id = get_file_id(dest_path)

    old_sub = item.get("subfolder", "").rstrip("/")
    new_sub = f"{old_sub}/{target_folder}" if old_sub else f"/{target_folder}"

    item["id"] = new_id
    item["path"] = dest_path
    item["fullPath"] = dest_path
    item["subfolder"] = new_sub
    item["directory"] = dest_dir

    updated_model = create_video_model(item, base_url=base_url).model_dump()

    with CACHE_LOCK:
        config.FILE_MAP.pop(file_id, None)
        config.FILE_MAP[new_id] = updated_model
        for idx, cat_item in enumerate(config.FILES_LIST):
            if cat_item.get("id") == file_id:
                config.FILES_LIST[idx] = updated_model
                break

    save_disk_cache()
    return JSONResponse(
        {"success": True, "message": "File moved successfully", "newId": new_id}
    )


def handle_rename_file(request, body):
    file_id = body.get("id")
    new_name = (body.get("newName") or "").strip()

    with CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

    src_path = item.get("path") or item.get("fullPath", "") if item else ""
    if not item or not new_name or not os.path.exists(src_path):
        raise HTTPException(status_code=400, detail="Invalid Parameters")

    ext = os.path.splitext(src_path)[1]
    dest_path = os.path.join(os.path.dirname(src_path), new_name + ext)

    if os.path.exists(dest_path):
        raise HTTPException(
            status_code=409, detail="A file with that name already exists"
        )

    os.rename(src_path, dest_path)

    base_url = str(request.base_url).rstrip("/") if hasattr(request, "base_url") else ""
    new_id = get_file_id(dest_path)

    item["id"] = new_id
    item["name"] = new_name
    item["title"] = new_name
    item["path"] = dest_path
    item["fullPath"] = dest_path

    updated_model = create_video_model(item, base_url=base_url).model_dump()

    with CACHE_LOCK:
        config.FILE_MAP.pop(file_id, None)
        config.FILE_MAP[new_id] = updated_model
        for idx, cat_item in enumerate(config.FILES_LIST):
            if cat_item.get("id") == file_id:
                config.FILES_LIST[idx] = updated_model
                break

    save_disk_cache()
    return JSONResponse(
        {
            "success": True,
            "message": "File renamed successfully",
            "newId": new_id,
        }
    )


def handle_delete_file(request, file_id):
    with CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

    if item:
        src_path = item.get("path") or item.get("fullPath")
        if src_path and os.path.exists(src_path):
            try:
                os.remove(src_path)
            except (PermissionError, FileNotFoundError, OSError) as ex:
                log(f"<!> Error deleting file: {type(ex).__name__}: {ex}")
                if isinstance(ex, PermissionError):
                    return JSONResponse(
                        {
                            "success": False,
                            "error": "Permission denied when deleting file.",
                        },
                        status_code=403,
                    )
                elif isinstance(ex, FileNotFoundError):
                    return JSONResponse(
                        {
                            "success": False,
                            "error": "File not found when attempting delete.",
                        },
                        status_code=404,
                    )
                else:
                    return JSONResponse(
                        {
                            "success": False,
                            "error": f"Unable to delete file: {type(ex).__name__}: {ex}",
                        },
                        status_code=500,
                    )

        with CACHE_LOCK:
            config.FILE_MAP.pop(file_id, None)
            config.FILES_LIST = [i for i in config.FILES_LIST if i.get("id") != file_id]

        save_disk_cache()
        return JSONResponse({"success": True, "message": "File deleted successfully"})

    return JSONResponse({"success": False, "error": "Invalid File ID"}, status_code=400)


def stream_video_file(request, file_path, send_body=True):
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File Not Found")

    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        raise HTTPException(status_code=404, detail="Unable to determine file size")

    if file_size <= 0:
        raise HTTPException(status_code=404, detail="Empty File")

    range_header = request.headers.get("Range") if hasattr(request, "headers") else None

    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match:
            return JSONResponse(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{file_size}",
                    "Accept-Ranges": "bytes",
                    "Connection": "keep-alive",
                },
                content={"error": "Requested Range Not Satisfiable"},
            )

        start_text, end_text = match.group(1), match.group(2)

        if start_text == "" and end_text != "":
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return JSONResponse(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{file_size}"},
                    content={"error": "Invalid byte range"},
                )
            start = max(0, file_size - min(suffix_length, file_size))
            end = file_size - 1
        else:
            if start_text == "":
                return JSONResponse(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{file_size}"},
                    content={"error": "Invalid byte range"},
                )
            start = int(start_text)
            if start >= file_size:
                return JSONResponse(
                    status_code=416,
                    headers={
                        "Content-Range": f"bytes */{file_size}",
                        "Accept-Ranges": "bytes",
                    },
                    content={"error": "Range start out of bounds"},
                )
            end = int(end_text) if end_text else file_size - 1
            end = min(end, file_size - 1)

            if end < start:
                return JSONResponse(
                    status_code=416,
                    headers={
                        "Content-Range": f"bytes */{file_size}",
                        "Accept-Ranges": "bytes",
                    },
                    content={"error": "Range end less than start"},
                )

        length = end - start + 1
        content_range = f"bytes {start}-{end}/{file_size}"

        def iter_file_range():
            try:
                with open(file_path, "rb") as f:
                    f.seek(start)
                    bytes_remaining = length
                    while bytes_remaining > 0:
                        chunk = f.read(min(CHUNK_SIZE, bytes_remaining))
                        if not chunk:
                            break
                        bytes_remaining -= len(chunk)
                        yield chunk
            except BrokenPipeError:
                log(f"--> Video client disconnected during range {start}-{end}.")

        return StreamingResponse(
            iter_file_range() if send_body else iter([]),
            status_code=206,
            headers={
                "Content-Range": content_range,
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Content-Type": "video/mp4",
            },
        )

    return FileResponse(
        file_path,
        status_code=200,
        headers={"Accept-Ranges": "bytes", "Content-Length": str(file_size)},
        media_type="video/mp4",
    )
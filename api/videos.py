import os
import re

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from config import CHUNK_SIZE, log


def stream_video_file(request: Request, file_path: str, send_body: bool = True):
    """
    Stream a video file with HTTP byte-range support.

    This function is intentionally responsible only for streaming
    the actual video bytes. Video metadata, thumbnails, catalog
    management, move, rename, and delete operations belong to the
    video model service.
    """

    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=404,
            detail="Video Not Found",
        )

    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        raise HTTPException(
            status_code=404,
            detail="Unable to determine video size",
        )

    if file_size <= 0:
        raise HTTPException(
            status_code=404,
            detail="Empty Video",
        )

    range_header = (
        request.headers.get("Range")
        if hasattr(request, "headers")
        else None
    )

    if range_header:
        match = re.fullmatch(
            r"bytes=(\d*)-(\d*)",
            range_header.strip(),
        )

        if not match:
            return JSONResponse(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{file_size}",
                    "Accept-Ranges": "bytes",
                    "Connection": "keep-alive",
                },
                content={
                    "error": "Requested Range Not Satisfiable",
                },
            )

        start_text, end_text = match.group(1), match.group(2)

        # ------------------------------------------------------------
        # Suffix byte range: bytes=-500
        # ------------------------------------------------------------
        if start_text == "" and end_text != "":
            suffix_length = int(end_text)

            if suffix_length <= 0:
                return JSONResponse(
                    status_code=416,
                    headers={
                        "Content-Range": f"bytes */{file_size}",
                        "Accept-Ranges": "bytes",
                    },
                    content={
                        "error": "Invalid byte range",
                    },
                )

            start = max(
                0,
                file_size - min(suffix_length, file_size),
            )
            end = file_size - 1

        # ------------------------------------------------------------
        # Normal byte range: bytes=500-999
        # or open-ended: bytes=500-
        # ------------------------------------------------------------
        else:
            if start_text == "":
                return JSONResponse(
                    status_code=416,
                    headers={
                        "Content-Range": f"bytes */{file_size}",
                        "Accept-Ranges": "bytes",
                    },
                    content={
                        "error": "Invalid byte range",
                    },
                )

            start = int(start_text)

            if start >= file_size:
                return JSONResponse(
                    status_code=416,
                    headers={
                        "Content-Range": f"bytes */{file_size}",
                        "Accept-Ranges": "bytes",
                    },
                    content={
                        "error": "Range start out of bounds",
                    },
                )

            end = (
                int(end_text)
                if end_text
                else file_size - 1
            )

            end = min(end, file_size - 1)

            if end < start:
                return JSONResponse(
                    status_code=416,
                    headers={
                        "Content-Range": f"bytes */{file_size}",
                        "Accept-Ranges": "bytes",
                    },
                    content={
                        "error": "Range end less than start",
                    },
                )

        length = end - start + 1
        content_range = f"bytes {start}-{end}/{file_size}"

        def iter_file_range():
            try:
                with open(file_path, "rb") as video_file:
                    video_file.seek(start)

                    bytes_remaining = length

                    while bytes_remaining > 0:
                        chunk = video_file.read(
                            min(CHUNK_SIZE, bytes_remaining)
                        )

                        if not chunk:
                            break

                        bytes_remaining -= len(chunk)
                        yield chunk

            except BrokenPipeError:
                log(
                    f"--> Video client disconnected during "
                    f"range {start}-{end}."
                )

            except OSError as ex:
                log(
                    f"<!> Video streaming read error: "
                    f"{type(ex).__name__}: {ex}"
                )

        return StreamingResponse(
            iter_file_range() if send_body else iter(()),
            status_code=206,
            headers={
                "Content-Range": content_range,
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Content-Type": "video/mp4",
            },
        )

    # ------------------------------------------------------------
    # No Range header.
    #
    # Return the complete video. FileResponse handles the actual
    # file delivery; this service still remains responsible only
    # for streaming the video itself.
    # ------------------------------------------------------------

    return FileResponse(
        file_path,
        status_code=200,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        },
        media_type="video/mp4",
    )
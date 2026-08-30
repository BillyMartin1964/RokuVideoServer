import time

from fastapi.responses import JSONResponse

import config
from config import CACHE_LOCK
from services import ffmpeg_service


def handle_get_health(request):
    with CACHE_LOCK:
        total_videos = len(config.FILES_LIST)

    ffmpeg_path = ffmpeg_service.FFMPEG_PATH
    ffprobe_path = ffmpeg_service.FFPROBE_PATH

    return JSONResponse(
        {
            "success": True,
            "server": "Roku Media Hub",
            "ffmpegFound": ffmpeg_path is not None,
            "ffmpegPath": ffmpeg_path,
            "ffprobeFound": ffprobe_path is not None,
            "ffprobePath": ffprobe_path,
            "thumbnailDirectory": config.THUMB_CACHE_DIR,
            "videoCount": total_videos,
            "uptimeSeconds": round(
                time.time() - config.SERVER_START_TIME,
                1,
            ),
        }
    )

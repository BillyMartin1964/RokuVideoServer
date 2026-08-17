import os
import subprocess
import time
import services.ffmpeg_service as ffmpeg_service
from config import (
    THUMB_CACHE_DIR,
    DEFAULT_POSTER_FILE,
    THUMB_WIDTH,
    THUMB_HEIGHT,
    THUMBNAIL_TIMEOUT_SECONDS,
    THUMBNAIL_SEEK_SECONDS,
    log,
    log_separator,
)
from services.video_service import get_file_id


def thumbnail_cache_path(file_path):
    file_id = get_file_id(file_path)
    return os.path.join(THUMB_CACHE_DIR, f"{file_id}.jpg")


def create_default_poster_with_ffmpeg():
    if not ffmpeg_service.FFMPEG_PATH:
        return False
    try:
        cmd = [
            ffmpeg_service.FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x30343B:s={THUMB_WIDTH}x{THUMB_HEIGHT}",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            DEFAULT_POSTER_FILE,
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        return (
            result.returncode == 0
            and os.path.exists(DEFAULT_POSTER_FILE)
            and os.path.getsize(DEFAULT_POSTER_FILE) > 0
        )
    except Exception:
        return False


def create_default_poster_with_sips():
    if os.path.exists(DEFAULT_POSTER_FILE):
        return True
    temp_ppm = os.path.join(THUMB_CACHE_DIR, "_default_poster_source.ppm")
    try:
        rgb = bytes([48, 52, 59])
        with open(temp_ppm, "wb") as f:
            f.write(
                f"P6\n{THUMB_WIDTH} {THUMB_HEIGHT}\n255\n".encode("ascii")
            )
            f.write(rgb * (THUMB_WIDTH * THUMB_HEIGHT))

        result = subprocess.run(
            [
                "/usr/bin/sips",
                "-s",
                "format",
                "jpeg",
                temp_ppm,
                "--out",
                DEFAULT_POSTER_FILE,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        return (
            result.returncode == 0
            and os.path.exists(DEFAULT_POSTER_FILE)
            and os.path.getsize(DEFAULT_POSTER_FILE) > 0
        )
    except Exception:
        return False
    finally:
        if os.path.exists(temp_ppm):
            try:
                os.remove(temp_ppm)
            except Exception:
                pass


def ensure_default_poster():
    if (
        os.path.exists(DEFAULT_POSTER_FILE)
        and os.path.getsize(DEFAULT_POSTER_FILE) > 0
    ):
        return True
    if create_default_poster_with_ffmpeg():
        return True
    return create_default_poster_with_sips()


def run_ffmpeg_thumbnail(file_path, thumb_path, seek_seconds):
    if not ffmpeg_service.FFMPEG_PATH:
        return False
    cmd = [
        ffmpeg_service.FFMPEG_PATH,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(seek_seconds),
        "-i",
        file_path,
        "-frames:v",
        "1",
        "-vf",
        f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=decrease,pad={THUMB_WIDTH}:{THUMB_HEIGHT}:(ow-iw)/2:(oh-ih)/2",
        "-q:v",
        "3",
        "-y",
        thumb_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=THUMBNAIL_TIMEOUT_SECONDS,
        )
        return (
            result.returncode == 0
            and os.path.exists(thumb_path)
            and os.path.getsize(thumb_path) > 0
        )
    except Exception:
        return False


def run_quicklook_thumbnail(file_path, thumb_path):
    try:
        before_files = set(os.listdir(THUMB_CACHE_DIR))
        cmd = [
            "/usr/bin/qlmanage",
            "-t",
            "-s",
            str(THUMB_WIDTH),
            "-o",
            THUMB_CACHE_DIR,
            file_path,
        ]
        subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=THUMBNAIL_TIMEOUT_SECONDS,
        )
        after_files = set(os.listdir(THUMB_CACHE_DIR))
        new_files = [
            f
            for f in (after_files - before_files)
            if f.lower().endswith(".png")
        ]

        for filename in new_files:
            source_thumbnail = os.path.join(THUMB_CACHE_DIR, filename)
            if (
                os.path.exists(source_thumbnail)
                and os.path.getsize(source_thumbnail) > 0
            ):
                convert_result = subprocess.run(
                    [
                        "/usr/bin/sips",
                        "-s",
                        "format",
                        "jpeg",
                        source_thumbnail,
                        "--out",
                        thumb_path,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=10,
                )
                if (
                    convert_result.returncode == 0
                    and os.path.exists(thumb_path)
                    and os.path.getsize(thumb_path) > 0
                ):
                    try:
                        os.remove(source_thumbnail)
                    except Exception:
                        pass
                    return True
    except Exception:
        pass
    return False


def generate_thumbnail(file_path):
    thumb_path = thumbnail_cache_path(file_path)
    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
        return thumb_path

    log_separator()
    log(f"THUMBNAIL REQUEST: {os.path.basename(file_path)}")

    if ffmpeg_service.FFMPEG_PATH:
        if run_ffmpeg_thumbnail(
            file_path, thumb_path, THUMBNAIL_SEEK_SECONDS
        ):
            return thumb_path
        if run_ffmpeg_thumbnail(file_path, thumb_path, 0):
            return thumb_path

    if run_quicklook_thumbnail(file_path, thumb_path):
        return thumb_path

    if ensure_default_poster():
        return DEFAULT_POSTER_FILE

    return None
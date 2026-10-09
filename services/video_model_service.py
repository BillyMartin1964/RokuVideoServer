import os
import math
import threading
import shutil
import subprocess
import tempfile

from config import (
    DEFAULT_POSTER_FILE,
    THUMB_CACHE_DIR,
    THUMB_HEIGHT,
    THUMB_WIDTH,
    THUMBNAIL_SEEK_SECONDS,
    THUMBNAIL_TIMEOUT_SECONDS,
    log,
    log_separator,
)
from services import ffmpeg_service
from services.video_service import get_file_id


FFMPEG_THUMBNAIL_FATAL_STRUCTURE = "fatal_structure"
FFMPEG_THUMBNAIL_FATAL_DECODER = "fatal_decoder"


def _has_fatal_mp4_structure_error(stderr):
    """Recognize container-structure failures that cannot be fixed by reseeking."""
    error_text = (stderr or "").lower()
    return any(
        marker in error_text
        for marker in (
            "moov atom not found",
            "missing mandatory atoms",
            "contradictionary stsc and stco",
            "broken header",
        )
    )


def _has_fatal_decoder_error(stderr):
    """Recognize decoder/filter failures that cannot be fixed by reseeking."""
    error_text = (stderr or "").lower()
    decoder_markers = (
        "reference picture missing",
        "missing reference picture",
        "mmco:",
        "number of reference frames",
        "invalid nal unit size",
        "missing picture in access unit",
        "error splitting the input into nal units",
        "decoding error",
        "cannot determine format of input",
    )
    return (
        any(marker in error_text for marker in decoder_markers)
        or (
            "padded dimensions cannot be smaller than input dimensions" in error_text
            and "failed to configure input pad" in error_text
        )
    )


def _fatal_thumbnail_error_kind(stderr):
    if _has_fatal_mp4_structure_error(stderr):
        return FFMPEG_THUMBNAIL_FATAL_STRUCTURE
    # Decoder damage can be local to one seek position; try another position.
    return None


def thumbnail_cache_path(file_path):
    file_id = get_file_id(file_path)
    # Keep legacy thumbnails intact, but regenerate them with timed capture.
    return os.path.join(THUMB_CACHE_DIR, f"{file_id}.timed-v2.jpg")


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
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        return (
            result.returncode == 0
            and os.path.exists(DEFAULT_POSTER_FILE)
            and os.path.getsize(DEFAULT_POSTER_FILE) > 0
        )

    except (
        OSError,
        subprocess.SubprocessError,
        TimeoutError,
    ) as ex:
        log(f"<!> FFmpeg default poster creation failed: {type(ex).__name__}: {ex}")
        return False


def create_default_poster_with_sips():
    if os.path.exists(DEFAULT_POSTER_FILE):
        return True

    temp_ppm = os.path.join(
        THUMB_CACHE_DIR,
        "_default_poster_source.ppm",
    )

    try:
        rgb = bytes([48, 52, 59])

        with open(temp_ppm, "wb") as f:
            f.write(f"P6\n{THUMB_WIDTH} {THUMB_HEIGHT}\n255\n".encode("ascii"))
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
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        return (
            result.returncode == 0
            and os.path.exists(DEFAULT_POSTER_FILE)
            and os.path.getsize(DEFAULT_POSTER_FILE) > 0
        )

    except (
        OSError,
        subprocess.SubprocessError,
        TimeoutError,
    ) as ex:
        log(f"<!> SIPS default poster creation failed: {type(ex).__name__}: {ex}")
        return False

    finally:
        if os.path.exists(temp_ppm):
            try:
                os.remove(temp_ppm)
            except OSError:
                pass


def ensure_default_poster():
    if os.path.exists(DEFAULT_POSTER_FILE) and os.path.getsize(DEFAULT_POSTER_FILE) > 0:
        return True

    if create_default_poster_with_ffmpeg():
        return True

    return create_default_poster_with_sips()


def run_ffmpeg_thumbnail(file_path, thumb_path, seek_seconds):
    """Select a nonblack frame in a three-second window, publishing atomically."""
    if not ffmpeg_service.FFMPEG_PATH:
        return False

    # The second attempt decodes five seconds of preroll to recover from a
    # damaged keyframe. Seek BEFORE input; trim AFTER seeking, never after
    # trimming the opening three seconds of the entire source.
    for preroll in (0, min(5.0, seek_seconds)):
        temporary_path = None
        try:
            fd, temporary_path = tempfile.mkstemp(
                suffix=".jpg", prefix=".capture-", dir=os.path.dirname(thumb_path)
            )
            os.close(fd)
            filters = (
                f"trim=start={preroll}:duration=3,setpts=PTS-STARTPTS,"
                "blackframe=amount=0:threshold=32,"
                "metadata=mode=select:key=lavfi.blackframe.pblack:value=95:function=less,"
                f"scale={THUMB_WIDTH}:{THUMB_HEIGHT - 1}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                "format=yuvj444p,"
                f"pad={THUMB_WIDTH}:{THUMB_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
                "setsar=1,format=yuvj420p"
            )
            cmd = [
                ffmpeg_service.FFMPEG_PATH, "-hide_banner", "-loglevel", "error",
                "-nostdin", "-ss", str(max(0, seek_seconds - preroll)),
                "-i", file_path, "-map", "0:v:0", "-an", "-sn",
                "-frames:v", "1", "-vf", filters, "-threads:v", "1",
                "-q:v", "3", "-strict", "unofficial", "-y", temporary_path,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=THUMBNAIL_TIMEOUT_SECONDS, check=False,
            )
            if result.returncode == 0 and os.path.getsize(temporary_path) > 0:
                os.replace(temporary_path, thumb_path)
                return True

            error = (result.stderr or "").strip()
            fatal_kind = _fatal_thumbnail_error_kind(error)
            if fatal_kind:
                log(
                    f"<!> Cannot read video container for {os.path.basename(file_path)}; "
                    "repair or replace the source file. " + error[:300]
                )
                return fatal_kind
            if result.returncode == 0:
                log(f"--> No nonblack frame near {seek_seconds:.1f}s: {os.path.basename(file_path)}")
                return False
            log(
                f"<!> Thumbnail capture at {seek_seconds:.1f}s "
                f"(preroll {preroll:.1f}s) failed: {os.path.basename(file_path)}: "
                + error[:300]
            )
        except (OSError, subprocess.SubprocessError, TimeoutError) as ex:
            log(f"<!> Thumbnail capture failed: {os.path.basename(file_path)}: {ex}")
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.remove(temporary_path)
        if seek_seconds == 0:
            break
    return False


def run_quicklook_thumbnail(
    file_path,
    thumb_path,
):
    # Isolated temp directory per thread/process prevents
    # collision during parallel catalog generation.
    try:
        temp_dir = tempfile.mkdtemp(dir=THUMB_CACHE_DIR)
    except OSError as ex:
        log(
            f"<!> QuickLook temporary directory creation failed for "
            f"{os.path.basename(file_path)}: "
            f"{type(ex).__name__}: {ex}"
        )
        return False

    try:
        cmd = [
            "/usr/bin/qlmanage",
            "-t",
            "-s",
            str(THUMB_WIDTH),
            "-o",
            temp_dir,
            file_path,
        ]

        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=THUMBNAIL_TIMEOUT_SECONDS,
            check=False,
        )

        generated_pngs = [
            filename
            for filename in os.listdir(temp_dir)
            if filename.lower().endswith(".png")
        ]

        if not generated_pngs:
            return False

        source_thumbnail = os.path.join(
            temp_dir,
            generated_pngs[0],
        )

        if os.path.exists(source_thumbnail) and os.path.getsize(source_thumbnail) > 0:
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
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            return (
                convert_result.returncode == 0
                and os.path.exists(thumb_path)
                and os.path.getsize(thumb_path) > 0
            )

    except (
        OSError,
        subprocess.SubprocessError,
        TimeoutError,
    ) as ex:
        log(
            f"<!> QuickLook thumbnail failed for "
            f"{os.path.basename(file_path)}: "
            f"{type(ex).__name__}: {ex}"
        )

    finally:
        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )

    return False


# Serialize requests for the same source without retaining a lock per video.
_THUMBNAIL_LOCKS = [threading.Lock() for _ in range(64)]
_FAILED_SOURCES = {}


def generate_thumbnail(file_path):
    with _THUMBNAIL_LOCKS[hash(os.path.abspath(file_path)) % len(_THUMBNAIL_LOCKS)]:
        return _generate_thumbnail(file_path)


def _generate_thumbnail(file_path):
    thumb_path = thumbnail_cache_path(file_path)

    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
        return thumb_path

    source_stat = os.stat(file_path)
    source_signature = (source_stat.st_size, source_stat.st_mtime_ns)
    if _FAILED_SOURCES.get(file_path) == source_signature:
        return DEFAULT_POSTER_FILE if ensure_default_poster() else None

    log_separator()
    log(f"THUMBNAIL REQUEST: {os.path.basename(file_path)}")

    if ffmpeg_service.FFMPEG_PATH:
        # Probe once for duration so we can avoid seeking past EOF on short videos.
        metadata = ffmpeg_service.probe_video_metadata(file_path)

        duration = metadata.get("duration") if isinstance(metadata, dict) else None

        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None

        if duration is not None and (not math.isfinite(duration) or duration <= 0):
            duration = None

        # Stay beyond the opening preview. For short clips use the middle
        # and later portions, leaving some room before EOF.
        base_seek = float(THUMBNAIL_SEEK_SECONDS)
        if duration and 0 < duration <= base_seek + 3:
            seek_times = [duration * fraction for fraction in (0.5, 0.6, 0.7, 0.8, 0.9)]
        else:
            seek_times = [base_seek + offset for offset in (0, 9, 18, 27, 45, 60, 90)]
            if duration and duration > 0:
                seek_times = [seek for seek in seek_times if seek < duration - 1]

        for seek_seconds in seek_times:
            log(
                f"--> Trying thumbnail at {seek_seconds:.1f} seconds for "
                f"'{os.path.basename(file_path)}'..."
            )

            thumbnail_result = run_ffmpeg_thumbnail(
                file_path,
                thumb_path,
                seek_seconds,
            )
            if thumbnail_result == FFMPEG_THUMBNAIL_FATAL_STRUCTURE:
                break
            if thumbnail_result == FFMPEG_THUMBNAIL_FATAL_DECODER:
                break
            if thumbnail_result:
                log(
                    f"--> Thumbnail generated successfully at "
                    f"({seek_seconds:.1f} seconds) for "
                    f"'{os.path.basename(file_path)}'"
                )
                return thumb_path

    _FAILED_SOURCES[file_path] = source_signature

    # QuickLook does not accept a seek time and may return an opening preview.
    # Use the neutral poster when no suitable timed frame can be extracted.
    if ensure_default_poster():
        return DEFAULT_POSTER_FILE

    return None

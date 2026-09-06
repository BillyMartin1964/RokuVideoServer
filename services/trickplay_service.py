import os
import shutil
import subprocess
import tempfile

import config
from config import log

TRICKPLAY_INTERVAL_SECONDS = 10
TRICKPLAY_WIDTH = 320
TRICKPLAY_HEIGHT = 180
TRICKPLAY_TIMEOUT_SECONDS = 3600

FFMPEG_PATH = None


def find_ffmpeg():
    """Find a usable FFmpeg executable using paths defined in config.py."""

    configured_path = getattr(config, "FFMPEG_PATH", None)

    if (
        configured_path
        and os.path.isfile(configured_path)
        and os.access(configured_path, os.X_OK)
    ):
        return configured_path

    configured_paths = getattr(config, "FFMPEG_PATHS", [])

    for path in configured_paths:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    return shutil.which("ffmpeg")


def initialize_trickplay():
    """Find and verify FFmpeg for trick-play generation."""

    global FFMPEG_PATH

    FFMPEG_PATH = find_ffmpeg()

    if not FFMPEG_PATH:
        log("<!> FFmpeg NOT FOUND. Trick-play generation is disabled.")
        return False

    log(f"--> Found FFmpeg for trick-play at: {FFMPEG_PATH}")

    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        if result.returncode == 0:
            log("--> FFmpeg is ready for trick-play generation.")
            return True

        log(f"<!> FFmpeg test failed with exit code {result.returncode}")

    except (
        subprocess.SubprocessError,
        OSError,
        ValueError,
    ) as ex:
        log(f"<!> FFmpeg test failed: {type(ex).__name__}: {ex}")

    FFMPEG_PATH = None

    return False


def get_trickplay_cache_dir(file_id):
    """Return the trick-play cache directory for one video."""

    if not file_id:
        raise ValueError("file_id is required")

    return os.path.join(
        config.TRICKPLAY_CACHE_DIR,
        str(file_id),
    )


def get_trickplay_frame_path(file_id, frame_number):
    """Return the deterministic JPEG path for a trick-play frame.

    Frame numbering is zero-based:

        000000.jpg = 0 seconds
        000001.jpg = 10 seconds
        000002.jpg = 20 seconds
        etc.
    """

    if not file_id:
        raise ValueError("file_id is required")

    try:
        frame_number = int(frame_number)
    except (TypeError, ValueError) as ex:
        raise ValueError("frame_number must be an integer") from ex

    if frame_number < 0:
        raise ValueError("frame_number must be zero or greater")

    cache_dir = get_trickplay_cache_dir(file_id)

    filename = f"{frame_number:06d}.jpg"

    return os.path.join(
        cache_dir,
        filename,
    )


def generate_trickplay(file_id, video_path):
    """Generate JPEG trick-play frames for one video.

    Frames are generated every TRICKPLAY_INTERVAL_SECONDS and stored
    under config.TRICKPLAY_CACHE_DIR/<file_id>/.

    The first frame is always numbered 000000.jpg.
    """

    if not file_id:
        log(
            f"<!> Trick-play generation skipped because video ID is empty: {video_path}"
        )
        return False

    if not video_path or not os.path.isfile(video_path):
        log(
            f"<!> Trick-play generation skipped because "
            f"video file does not exist: {video_path}"
        )
        return False

    if not FFMPEG_PATH and not initialize_trickplay():
        return False

    try:
        final_directory = get_trickplay_cache_dir(file_id)
    except (OSError, ValueError, TypeError) as ex:
        log(
            f"<!> Could not determine trick-play cache directory "
            f"for video ID {file_id}: "
            f"{type(ex).__name__}: {ex}"
        )
        return False

    try:
        if os.path.isdir(final_directory):
            existing_files = [
                name
                for name in os.listdir(final_directory)
                if name.lower().endswith(".jpg")
                and os.path.isfile(os.path.join(final_directory, name))
                and os.path.getsize(os.path.join(final_directory, name)) > 0
            ]

            if existing_files:
                log(
                    f"--> Trick-play cache already exists for "
                    f"{os.path.basename(video_path)} "
                    f"({len(existing_files)} JPEGs)"
                )
                return True

    except OSError as ex:
        log(
            f"<!> Could not inspect existing trick-play cache for "
            f"{os.path.basename(video_path)}: "
            f"{type(ex).__name__}: {ex}"
        )

    cache_root = config.TRICKPLAY_CACHE_DIR

    try:
        os.makedirs(cache_root, exist_ok=True)
    except OSError as ex:
        log(
            f"<!> Could not create trick-play cache root: "
            f"{cache_root}: "
            f"{type(ex).__name__}: {ex}"
        )
        return False

    try:
        with tempfile.TemporaryDirectory(
            prefix=f"{file_id}_",
            dir=cache_root,
        ) as temporary_directory:
            output_pattern = os.path.join(
                temporary_directory,
                "%06d.jpg",
            )

            filter_expression = (
                f"fps=1/{TRICKPLAY_INTERVAL_SECONDS},"
                f"scale={TRICKPLAY_WIDTH}:{TRICKPLAY_HEIGHT}:"
                f"force_original_aspect_ratio=decrease,"
                f"pad={TRICKPLAY_WIDTH}:{TRICKPLAY_HEIGHT}:"
                f"(ow-iw)/2:(oh-ih)/2"
            )

            command = [
                FFMPEG_PATH,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                video_path,
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-vf",
                filter_expression,
                "-q:v",
                "4",
                "-start_number",
                "0",
                output_pattern,
            ]

            log(
                f"--> Generating trick-play thumbnails every "
                f"{TRICKPLAY_INTERVAL_SECONDS} seconds for "
                f"'{os.path.basename(video_path)}'..."
            )

            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=TRICKPLAY_TIMEOUT_SECONDS,
                check=False,
            )

            if result.returncode != 0:
                log(
                    f"<!> FFmpeg trick-play generation failed for "
                    f"{os.path.basename(video_path)} "
                    f"with exit code {result.returncode}"
                )

                if result.stderr:
                    log(f"<!> FFmpeg stderr: {result.stderr.strip()}")

                return False

            generated_files = []

            try:
                temporary_files = sorted(os.listdir(temporary_directory))
            except OSError as ex:
                log(
                    f"<!> Could not inspect FFmpeg output directory "
                    f"for {os.path.basename(video_path)}: "
                    f"{type(ex).__name__}: {ex}"
                )
                return False

            for file_name in temporary_files:
                if not file_name.lower().endswith(".jpg"):
                    continue

                file_path = os.path.join(
                    temporary_directory,
                    file_name,
                )

                try:
                    if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                        generated_files.append(file_path)
                except OSError:
                    continue

            if not generated_files:
                log(
                    f"<!> FFmpeg completed but produced no JPEG "
                    f"trick-play thumbnails for "
                    f"{os.path.basename(video_path)}"
                )
                return False

            if os.path.exists(final_directory):
                shutil.rmtree(final_directory)

            os.makedirs(
                final_directory,
                exist_ok=True,
            )

            for source_path in generated_files:
                destination_path = os.path.join(
                    final_directory,
                    os.path.basename(source_path),
                )

                shutil.copy2(
                    source_path,
                    destination_path,
                )

            log(
                f"--> Generated {len(generated_files)} trick-play "
                f"thumbnails for "
                f"'{os.path.basename(video_path)}'"
            )

            return True

    except subprocess.TimeoutExpired:
        log(
            f"<!> Trick-play generation timed out after "
            f"{TRICKPLAY_TIMEOUT_SECONDS}s for "
            f"{os.path.basename(video_path)}"
        )

    except (
        OSError,
        shutil.Error,
        ValueError,
    ) as ex:
        log(
            f"<!> Trick-play generation error for "
            f"{os.path.basename(video_path)}: "
            f"{type(ex).__name__}: {ex}"
        )

    return False


def get_trickplay_thumbnail(file_id, timestamp_seconds):
    """Return a trick-play JPEG using a timestamp.

    This compatibility helper maps a timestamp directly to the
    corresponding 10-second frame.
    """

    if not file_id:
        return None

    try:
        timestamp = int(timestamp_seconds)
    except (TypeError, ValueError):
        return None

    if timestamp < 0:
        return None

    frame_number = timestamp // TRICKPLAY_INTERVAL_SECONDS

    try:
        thumbnail_path = get_trickplay_frame_path(
            file_id,
            frame_number,
        )
    except (OSError, ValueError, TypeError):
        return None

    try:
        if os.path.isfile(thumbnail_path) and os.path.getsize(thumbnail_path) > 0:
            return thumbnail_path
    except OSError:
        return None

    return None


def delete_trickplay_cache(file_id):
    """Delete all trick-play JPEGs for one video."""

    if not file_id:
        return False

    try:
        cache_directory = get_trickplay_cache_dir(file_id)
    except (OSError, ValueError, TypeError):
        return False

    if not os.path.exists(cache_directory):
        return True

    try:
        shutil.rmtree(cache_directory)

        log(f"--> Deleted trick-play cache for video ID {file_id}")

        return True

    except OSError as ex:
        log(
            f"<!> Failed to delete trick-play cache for "
            f"video ID {file_id}: "
            f"{type(ex).__name__}: {ex}"
        )

        return False

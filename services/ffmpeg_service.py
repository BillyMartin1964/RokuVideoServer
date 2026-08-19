import json
import os
import shutil
import subprocess

from config import FFPROBE_TIMEOUT_SECONDS, log

FFMPEG_PATH = None
FFPROBE_PATH = None


def find_ffmpeg():
    candidates = [
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        "/opt/local/bin/ffmpeg",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return shutil.which("ffmpeg")


def find_ffprobe():
    candidates = []
    if FFMPEG_PATH:
        candidates.append(
            os.path.join(os.path.dirname(FFMPEG_PATH), "ffprobe")
        )
    candidates.extend(
        [
            "/opt/homebrew/bin/ffprobe",
            "/usr/local/bin/ffprobe",
            "/usr/bin/ffprobe",
            "/opt/local/bin/ffprobe",
        ]
    )
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return shutil.which("ffprobe")


def test_ffmpeg(ffmpeg_path):
    if not ffmpeg_path:
        return False
    try:
        result = subprocess.run(
            [ffmpeg_path, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            first_line = (
                result.stdout.splitlines()[0] if result.stdout else "ffmpeg"
            )
            log(f"--> ffmpeg test successful: {first_line}")
            return True
        log(f"<!> ffmpeg exists but returned exit code {result.returncode}")
    except Exception as ex:
        log(f"<!> ffmpeg executable test failed: {type(ex).__name__}: {ex}")
    return False


def test_ffprobe(ffprobe_path):
    if not ffprobe_path:
        return False
    try:
        result = subprocess.run(
            [ffprobe_path, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            first_line = (
                result.stdout.splitlines()[0] if result.stdout else "ffprobe"
            )
            log(f"--> ffprobe test successful: {first_line}")
            return True
        log(f"<!> ffprobe exists but returned exit code {result.returncode}")
    except Exception as ex:
        log(f"<!> ffprobe executable test failed: {type(ex).__name__}: {ex}")
    return False


def initialize_ffmpeg():
    global FFMPEG_PATH, FFPROBE_PATH
    FFMPEG_PATH = find_ffmpeg()
    if not FFMPEG_PATH:
        log("<!> ffmpeg NOT FOUND.")
        log("<!> Thumbnails will attempt macOS QuickLook fallback.")
    else:
        log(f"--> Found ffmpeg binary at: {FFMPEG_PATH}")
        if test_ffmpeg(FFMPEG_PATH):
            log("--> ffmpeg is ready for thumbnail generation.")
        else:
            log("<!> ffmpeg was found but could not be executed.")

    FFPROBE_PATH = find_ffprobe()
    if not FFPROBE_PATH:
        log("<!> ffprobe NOT FOUND. Metadata will be limited.")
    else:
        log(f"--> Found ffprobe binary at: {FFPROBE_PATH}")
        if test_ffprobe(FFPROBE_PATH):
            log("--> ffprobe is ready for media metadata.")
        else:
            FFPROBE_PATH = None


def probe_video_metadata(file_path):
    metadata = {}
    if not FFPROBE_PATH:
        return metadata

    try:
        command = [
            FFPROBE_PATH,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,codec_name,bit_rate,r_frame_rate,pix_fmt,profile,level",
            "-show_entries",
            "format=duration,bit_rate",
            "-of",
            "json",
            file_path,
        ]
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0 or not result.stdout:
            return metadata

        probe_json = json.loads(result.stdout)
        streams = probe_json.get("streams", [])

        if streams:
            stream = streams[0]
            if stream.get("width") is not None:
                try:
                    metadata["width"] = int(stream["width"])
                except Exception:
                    pass
            if stream.get("height") is not None:
                try:
                    metadata["height"] = int(stream["height"])
                except Exception:
                    pass
            if stream.get("codec_name"):
                metadata["codec"] = stream["codec_name"]
            if stream.get("bit_rate"):
                try:
                    metadata["bitrate"] = int(stream["bit_rate"])
                except Exception:
                    pass
            if stream.get("r_frame_rate"):
                frame_rate = stream["r_frame_rate"]
                if isinstance(frame_rate, str) and "/" in frame_rate:
                    try:
                        num, den = frame_rate.split("/", 1)
                        if float(den) != 0:
                            metadata["frameRate"] = round(
                                float(num) / float(den), 3
                            )
                    except Exception:
                        pass
                else:
                    try:
                        metadata["frameRate"] = float(frame_rate)
                    except Exception:
                        pass
            if stream.get("pix_fmt"):
                metadata["pixelFormat"] = stream["pix_fmt"]
            if stream.get("profile"):
                metadata["profile"] = stream["profile"]
            if stream.get("level") is not None:
                try:
                    metadata["level"] = int(stream["level"])
                except Exception:
                    pass

        format_info = probe_json.get("format", {})
        if format_info.get("duration"):
            try:
                metadata["duration"] = float(format_info["duration"])
            except Exception:
                pass
        if "bitrate" not in metadata and format_info.get("bit_rate"):
            try:
                metadata["bitrate"] = int(format_info["bit_rate"])
            except Exception:
                pass

    except subprocess.TimeoutExpired:
        log(f"<!> ffprobe timed out for: {os.path.basename(file_path)}")
    except Exception:
        pass

    return metadata
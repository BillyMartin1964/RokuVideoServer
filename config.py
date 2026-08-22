import logging
import os
import sys
import threading
import time

# Network & Server Settings
PORT = 8001
VOLUMES_DIR = "/Volumes"

# Cache Directories & Files
THUMB_CACHE_DIR = "/tmp/roku_thumbs"
FILE_CACHE_FILE = "/tmp/roku_files_cache.json"
DEFAULT_POSTER_FILE = os.path.join(THUMB_CACHE_DIR, "default_poster.jpg")
PLAYBACK_POSITIONS_FILE = os.path.join(
    os.path.dirname(__file__), "data", "playback.json"
)
LOG_FILE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "server_activity.log"
)

# FFmpeg / FFprobe Paths
FFMPEG_PATHS = [
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
]

FFPROBE_PATHS = [
    "/opt/homebrew/bin/ffprobe",
    "/usr/local/bin/ffprobe",
    "/usr/bin/ffprobe",
]

# Stream & Scan Settings
CHUNK_SIZE = 512 * 1024
REFRESH_INTERVAL_SECONDS = 600

# Thumbnail Settings

# THUMB_WIDTH = 384
# THUMB_HEIGHT = 216

THUMB_WIDTH = 592
THUMB_HEIGHT = 333

THUMBNAIL_TIMEOUT_SECONDS = 15
THUMBNAIL_SEEK_SECONDS = 60
FFPROBE_TIMEOUT_SECONDS = 5

# Video Processing Filters
ALLOWED_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".m4v",
    ".webm",
    ".ts",
    ".flv",
}

IGNORED_DIRS = {
    ".trashes",
    ".spotlight-v100",
    ".fseventsd",
    ".temporaryitems",
    "system volume information",
    "$recycle.bin",
    ".git",
    ".cache",
    "backups.backupdb",
}

IGNORED_EXTENSIONS = {
    ".sparsebundle",
    ".backup",
    ".app",
    ".photoslibrary",
    ".kext",
}

VIDEO_FORMATS = {
    ".mp4": {"streamFormat": "mp4", "contentType": "video/mp4"},
    ".m4v": {"streamFormat": "mp4", "contentType": "video/mp4"},
    ".mov": {"streamFormat": "mp4", "contentType": "video/quicktime"},
    ".mkv": {"streamFormat": "mkv", "contentType": "video/x-matroska"},
    ".webm": {"streamFormat": "mkv", "contentType": "video/webm"},
    ".avi": {"streamFormat": "mp4", "contentType": "video/x-msvideo"},
    ".ts": {"streamFormat": "mp4", "contentType": "video/mp2t"},
    ".flv": {"streamFormat": "mp4", "contentType": "video/x-flv"},
}

# Global Shared State
CACHE_LOCK = threading.Lock()
FILE_MAP = {}
FILES_LIST = []
SCAN_IN_PROGRESS = False
SERVER_START_TIME = time.time()

# Ensure directories exist
os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
os.makedirs(os.path.dirname(PLAYBACK_POSITIONS_FILE), exist_ok=True)

import logging
from logging.handlers import TimedRotatingFileHandler

# Logger Setup
_logger = logging.getLogger("RokuServerNew")
_logger.setLevel(logging.INFO)

if not _logger.handlers:
    # Rotates daily at midnight and keeps a rolling 7-day log history
    _file_handler = TimedRotatingFileHandler(
        LOG_FILE_PATH,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8"
    )
    _file_handler.suffix = "%Y-%m-%d"
    _file_handler.setFormatter(logging.Formatter("%(message)s"))

    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setFormatter(logging.Formatter("%(message)s"))

    _logger.addHandler(_file_handler)
    _logger.addHandler(_console_handler)


def log(message):
    timestamp = time.strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {message}"
    _logger.info(formatted_msg)
    sys.stdout.flush()


def log_separator():
    _logger.info("")
    _logger.info("=" * 72)
    sys.stdout.flush()
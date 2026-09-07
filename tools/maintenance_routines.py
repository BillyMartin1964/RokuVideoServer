#!/usr/bin/env python3

"""
===============================================================================
MAINTENANCE_ROUTINES.PY
===============================================================================

Maintenance and validation routines for Roku Media Hub video assets.

This module complements the existing thumbnail worker in server.py.

Existing server.py responsibilities:
    - Queue missing thumbnails.
    - Generate thumbnails in the background.
    - Prevent duplicate thumbnail jobs.

This module responsibilities:
    - Validate thumbnail cache files.
    - Detect empty/corrupt/stale thumbnails.
    - Validate trick-play cache directories.
    - Detect missing/corrupt trick-play frames.
    - Detect orphaned cache assets.
    - Scan the indexed catalog.
    - Produce maintenance reports.
    - Optionally repair invalid assets.

The routines here are deliberately filesystem-oriented and reusable so they
can be called from:

    - FastAPI maintenance endpoints
    - Background maintenance workers
    - Startup maintenance checks
    - Administrative tools
    - Tests
===============================================================================
"""

from __future__ import annotations

import os
import shutil
import struct
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import config
from config import CACHE_LOCK, log
from services import trickplay_service, video_model_service

# ============================================================================
# MAINTENANCE CONFIGURATION
# ============================================================================


# JPEG files smaller than this are almost certainly invalid.
MIN_JPEG_FILE_SIZE = 128


# Small tolerance prevents false positives caused by filesystem timestamp
# resolution differences.
STALE_TIMESTAMP_TOLERANCE_SECONDS = 2


# Expected trick-play frame extension.
TRICKPLAY_EXTENSION = ".jpg"


# ============================================================================
# RESULT MODELS
# ============================================================================


@dataclass
class AssetValidationResult:
    """
    Result of validating one cached media asset.
    """

    valid: bool

    path: str | None = None

    exists: bool = False
    empty: bool = False
    corrupt: bool = False
    stale: bool = False

    size: int | None = None

    width: int | None = None
    height: int | None = None

    reason: str | None = None


@dataclass
class TrickplayValidationResult:
    """
    Result of validating all trick-play assets for one video.
    """

    file_id: str

    valid: bool

    cache_directory: str | None = None

    directory_exists: bool = False

    frame_count: int = 0

    valid_frame_count: int = 0

    invalid_frame_count: int = 0

    missing_frame_numbers: list[int] = field(default_factory=list)

    invalid_frames: list[str] = field(default_factory=list)

    frame_results: list[AssetValidationResult] = field(default_factory=list)

    reason: str | None = None


@dataclass
class VideoMaintenanceResult:
    """
    Complete maintenance result for one indexed video.
    """

    file_id: str

    file_path: str | None = None

    video_exists: bool = False

    thumbnail: AssetValidationResult | None = None

    trickplay: TrickplayValidationResult | None = None

    thumbnail_repaired: bool = False

    trickplay_repaired: bool = False

    errors: list[str] = field(default_factory=list)


# ============================================================================
# SERIALIZATION HELPERS
#
# These are intentionally defined BEFORE MaintenanceReport so Pylance can
# resolve them when MaintenanceReport.to_dict() is analyzed.
# ============================================================================


def asset_validation_result_to_dict(
    result: AssetValidationResult,
) -> dict[str, Any]:
    """
    Convert AssetValidationResult to a JSON-friendly dictionary.
    """

    return {
        "valid": result.valid,
        "path": result.path,
        "exists": result.exists,
        "empty": result.empty,
        "corrupt": result.corrupt,
        "stale": result.stale,
        "size": result.size,
        "width": result.width,
        "height": result.height,
        "reason": result.reason,
    }


def trickplay_validation_result_to_dict(
    result: TrickplayValidationResult,
) -> dict[str, Any]:
    """
    Convert TrickplayValidationResult to a JSON-friendly dictionary.
    """

    return {
        "fileId": result.file_id,
        "valid": result.valid,
        "cacheDirectory": result.cache_directory,
        "directoryExists": result.directory_exists,
        "frameCount": result.frame_count,
        "validFrameCount": result.valid_frame_count,
        "invalidFrameCount": result.invalid_frame_count,
        "missingFrameNumbers": result.missing_frame_numbers,
        "invalidFrames": result.invalid_frames,
        "reason": result.reason,
    }


def video_maintenance_result_to_dict(
    result: VideoMaintenanceResult,
) -> dict[str, Any]:
    """
    Convert VideoMaintenanceResult to a JSON-friendly dictionary.
    """

    return {
        "fileId": result.file_id,
        "filePath": result.file_path,
        "videoExists": result.video_exists,
        "thumbnail": (
            asset_validation_result_to_dict(result.thumbnail)
            if result.thumbnail is not None
            else None
        ),
        "trickplay": (
            trickplay_validation_result_to_dict(result.trickplay)
            if result.trickplay is not None
            else None
        ),
        "thumbnailRepaired": result.thumbnail_repaired,
        "trickplayRepaired": result.trickplay_repaired,
        "errors": result.errors,
    }


@dataclass
class MaintenanceReport:
    """
    Aggregate report for a catalog-wide maintenance scan.
    """

    started_at: float

    completed_at: float | None = None

    videos_scanned: int = 0

    videos_missing: int = 0

    videos_with_errors: int = 0

    thumbnails_valid: int = 0
    thumbnails_missing: int = 0
    thumbnails_empty: int = 0
    thumbnails_corrupt: int = 0
    thumbnails_stale: int = 0

    trickplay_valid: int = 0
    trickplay_missing: int = 0
    trickplay_invalid: int = 0

    orphaned_thumbnail_files: int = 0

    orphaned_trickplay_directories: int = 0

    thumbnails_repaired: int = 0

    trickplay_repaired: int = 0

    errors: list[str] = field(default_factory=list)

    video_results: list[VideoMaintenanceResult] = field(default_factory=list)

    def complete(self) -> None:
        """
        Mark the report as completed.
        """

        self.completed_at = time.time()

    def to_dict(
        self,
        include_video_results: bool = False,
    ) -> dict[str, Any]:
        """
        Convert the report into a JSON-friendly dictionary.
        """

        duration_seconds: float | None = None

        if self.completed_at is not None:
            duration_seconds = self.completed_at - self.started_at

        result: dict[str, Any] = {
            "success": len(self.errors) == 0,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "durationSeconds": duration_seconds,
            "videosScanned": self.videos_scanned,
            "videosMissing": self.videos_missing,
            "videosWithErrors": self.videos_with_errors,
            "thumbnails": {
                "valid": self.thumbnails_valid,
                "missing": self.thumbnails_missing,
                "empty": self.thumbnails_empty,
                "corrupt": self.thumbnails_corrupt,
                "stale": self.thumbnails_stale,
                "repaired": self.thumbnails_repaired,
            },
            "trickplay": {
                "valid": self.trickplay_valid,
                "missing": self.trickplay_missing,
                "invalid": self.trickplay_invalid,
                "repaired": self.trickplay_repaired,
            },
            "orphans": {
                "thumbnailFiles": (self.orphaned_thumbnail_files),
                "trickplayDirectories": (self.orphaned_trickplay_directories),
            },
            "errors": self.errors,
        }

        if include_video_results:
            result["videos"] = [
                video_maintenance_result_to_dict(item) for item in self.video_results
            ]

        return result


# ============================================================================
# BASIC FILE HELPERS
# ============================================================================


def safe_file_size(
    path: str,
) -> int | None:
    """
    Safely return a file's size.

    Returns None when the file cannot be inspected.
    """

    try:
        return os.path.getsize(path)

    except OSError:
        return None


def safe_file_mtime(
    path: str,
) -> float | None:
    """
    Safely return a file modification time.

    Returns None when the file cannot be inspected.
    """

    try:
        return os.path.getmtime(path)

    except OSError:
        return None


def safe_remove_file(
    path: str,
) -> bool:
    """
    Safely remove a file.

    Returns True only when the file was successfully removed.
    """

    try:
        if not os.path.isfile(path):
            return False

        os.remove(path)

        return True

    except OSError as ex:
        log(f"<!> Could not remove file {path}: {type(ex).__name__}: {ex}")

        return False


def safe_remove_directory(
    path: str,
) -> bool:
    """
    Safely remove a directory tree.

    Returns True only when the directory was successfully removed.
    """

    try:
        if not os.path.isdir(path):
            return False

        shutil.rmtree(path)

        return True

    except OSError as ex:
        log(f"<!> Could not remove directory {path}: {type(ex).__name__}: {ex}")

        return False


# ============================================================================
# JPEG VALIDATION
# ============================================================================


def get_jpeg_dimensions(
    file_path: str,
) -> tuple[int, int] | None:
    """
    Read JPEG dimensions without requiring Pillow.

    Returns:
        (width, height)

    Returns None when:

        - The file cannot be opened.
        - The file is not a recognizable JPEG.
        - Dimensions cannot be determined.
    """

    try:
        with open(
            file_path,
            "rb",
        ) as file_handle:
            # JPEG Start Of Image marker.
            marker = file_handle.read(2)

            if marker != b"\xff\xd8":
                return None

            while True:
                byte = file_handle.read(1)

                if not byte:
                    return None

                while byte != b"\xff":
                    byte = file_handle.read(1)

                    if not byte:
                        return None

                while byte == b"\xff":
                    byte = file_handle.read(1)

                    if not byte:
                        return None

                marker_code = byte[0]

                # Standalone JPEG markers.
                if marker_code in (
                    0xD8,
                    0xD9,
                ):
                    continue

                # Start Of Scan.
                #
                # Dimensions should have appeared before this.
                if marker_code == 0xDA:
                    return None

                length_data = file_handle.read(2)

                if len(length_data) != 2:
                    return None

                segment_length = struct.unpack(
                    ">H",
                    length_data,
                )[0]

                if segment_length < 2:
                    return None

                # JPEG Start Of Frame markers.
                if marker_code in (
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                ):
                    frame_data = file_handle.read(segment_length - 2)

                    if len(frame_data) < 5:
                        return None

                    height = struct.unpack(
                        ">H",
                        frame_data[1:3],
                    )[0]

                    width = struct.unpack(
                        ">H",
                        frame_data[3:5],
                    )[0]

                    if width <= 0 or height <= 0:
                        return None

                    return (
                        width,
                        height,
                    )

                file_handle.seek(
                    segment_length - 2,
                    os.SEEK_CUR,
                )

    except (
        OSError,
        ValueError,
        struct.error,
    ):
        return None


def validate_jpeg_file(
    file_path: str,
    source_file_path: str | None = None,
    check_stale: bool = False,
) -> AssetValidationResult:
    """
    Validate a JPEG cache file.

    Checks:

        - File exists.
        - File size.
        - JPEG structure.
        - Image dimensions.
        - Optional source timestamp freshness.
    """

    result = AssetValidationResult(
        valid=False,
        path=file_path,
    )

    if not file_path:
        result.reason = "JPEG path was not provided."

        return result

    if not os.path.isfile(file_path):
        result.reason = "JPEG file does not exist."

        return result

    result.exists = True

    file_size = safe_file_size(file_path)

    result.size = file_size

    if file_size is None or file_size < MIN_JPEG_FILE_SIZE:
        result.empty = True

        result.reason = "JPEG file is empty or too small."

        return result

    dimensions = get_jpeg_dimensions(file_path)

    if dimensions is None:
        result.corrupt = True

        result.reason = "JPEG file could not be validated."

        return result

    width, height = dimensions

    result.width = width

    result.height = height

    # ------------------------------------------------------------------------
    # Optional stale check.
    # ------------------------------------------------------------------------

    if check_stale and source_file_path and os.path.isfile(source_file_path):
        source_mtime = safe_file_mtime(source_file_path)

        jpeg_mtime = safe_file_mtime(file_path)

        if (
            source_mtime is not None
            and jpeg_mtime is not None
            and source_mtime > (jpeg_mtime + STALE_TIMESTAMP_TOLERANCE_SECONDS)
        ):
            result.stale = True

            result.reason = "JPEG cache file is older than its source video."

            return result

    result.valid = True

    result.reason = "JPEG file is valid."

    return result


# ============================================================================
# VIDEO LOOKUP
# ============================================================================


def get_catalog_snapshot() -> list[dict[str, Any]]:
    """
    Return a safe snapshot of indexed catalog items.
    """

    with CACHE_LOCK:
        catalog_items = list(config.FILES_LIST)

    return [
        item
        for item in catalog_items
        if isinstance(
            item,
            dict,
        )
    ]


def get_file_id_from_item(
    item: dict[str, Any],
) -> str:
    """
    Return the canonical file ID from a catalog item.
    """

    return str(item.get("id") or item.get("fileId") or "").strip()


def get_file_path_from_item(
    item: dict[str, Any],
) -> str:
    """
    Return the canonical filesystem path from a catalog item.
    """

    return str(item.get("path") or item.get("fullPath") or "").strip()


def get_indexed_video(
    file_id: str,
) -> dict[str, Any] | None:
    """
    Return one indexed video from FILE_MAP.
    """

    if not file_id:
        return None

    with CACHE_LOCK:
        item = config.FILE_MAP.get(file_id)

        if not isinstance(
            item,
            dict,
        ):
            return None

        return dict(item)


def get_indexed_video_path(
    file_id: str,
) -> str | None:
    """
    Return the filesystem path for one indexed video.
    """

    item = get_indexed_video(file_id)

    if item is None:
        return None

    file_path = get_file_path_from_item(item)

    if not file_path:
        return None

    return file_path


def get_all_indexed_file_ids() -> set[str]:
    """
    Return all currently indexed file IDs.
    """

    file_ids: set[str] = set()

    for item in get_catalog_snapshot():
        file_id = get_file_id_from_item(item)

        if file_id:
            file_ids.add(file_id)

    return file_ids


# ============================================================================
# THUMBNAIL VALIDATION
# ============================================================================


def get_thumbnail_path(
    file_path: str,
) -> str | None:
    """
    Determine the thumbnail cache path for a source video.
    """

    if not file_path:
        return None

    try:
        return video_model_service.thumbnail_cache_path(file_path)

    except (
        OSError,
        ValueError,
        TypeError,
    ) as ex:
        log(
            f"<!> Could not determine thumbnail "
            f"cache path for "
            f"{file_path}: "
            f"{type(ex).__name__}: {ex}"
        )

        return None


def validate_thumbnail(
    file_path: str,
) -> AssetValidationResult:
    """
    Validate the cached thumbnail for one source video.
    """

    thumbnail_path = get_thumbnail_path(file_path)

    if not thumbnail_path:
        return AssetValidationResult(
            valid=False,
            path=None,
            reason=("Could not determine thumbnail cache path."),
        )

    return validate_jpeg_file(
        file_path=thumbnail_path,
        source_file_path=file_path,
        check_stale=True,
    )


def remove_thumbnail_for_video(
    file_path: str,
) -> bool:
    """
    Remove the cached thumbnail for one video.
    """

    thumbnail_path = get_thumbnail_path(file_path)

    if not thumbnail_path:
        return False

    return safe_remove_file(thumbnail_path)


def repair_thumbnail(
    file_path: str,
) -> bool:
    """
    Repair the thumbnail for one video.

    Returns True only when a valid thumbnail exists after the repair.
    """

    if not file_path:
        return False

    if not os.path.isfile(file_path):
        return False

    validation = validate_thumbnail(file_path)

    if validation.valid:
        return True

    if validation.path:
        safe_remove_file(validation.path)

    try:
        generated_path = video_model_service.generate_thumbnail(file_path)

    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
    ) as ex:
        log(
            f"<!> Thumbnail repair failed for "
            f"{os.path.basename(file_path)}: "
            f"{type(ex).__name__}: {ex}"
        )

        return False

    if generated_path:
        validation = validate_jpeg_file(
            file_path=generated_path,
            source_file_path=file_path,
            check_stale=True,
        )

        return validation.valid

    # Some implementations may successfully generate the thumbnail
    # but return None. Validate the deterministic cache path.
    validation = validate_thumbnail(file_path)

    return validation.valid


# ============================================================================
# TRICK-PLAY HELPERS
# ============================================================================


def get_trickplay_directory(
    file_id: str,
) -> str | None:
    """
    Determine the trick-play cache directory for a video.
    """

    if not file_id:
        return None

    try:
        return trickplay_service.get_trickplay_cache_dir(file_id)

    except (
        OSError,
        ValueError,
        TypeError,
    ) as ex:
        log(
            f"<!> Could not determine trick-play "
            f"directory for "
            f"{file_id}: "
            f"{type(ex).__name__}: {ex}"
        )

        return None


def get_trickplay_frame_number(
    filename: str,
) -> int | None:
    """
    Parse a trick-play frame number from a filename.

    Expected examples:

        000000.jpg
        000001.jpg
        000002.jpg
    """

    if not filename:
        return None

    if not filename.lower().endswith(TRICKPLAY_EXTENSION):
        return None

    base_name = os.path.splitext(filename)[0]

    if not base_name.isdigit():
        return None

    try:
        return int(base_name)

    except ValueError:
        return None


def get_trickplay_frame_files(
    cache_directory: str,
) -> list[tuple[int, str]]:
    """
    Return all recognized trick-play frame files.
    """

    if not cache_directory:
        return []

    if not os.path.isdir(cache_directory):
        return []

    frames: list[tuple[int, str]] = []

    try:
        names = os.listdir(cache_directory)

    except OSError:
        return []

    for name in names:
        frame_number = get_trickplay_frame_number(name)

        if frame_number is None:
            continue

        frame_path = os.path.join(
            cache_directory,
            name,
        )

        if not os.path.isfile(frame_path):
            continue

        frames.append(
            (
                frame_number,
                frame_path,
            )
        )

    frames.sort(key=lambda item: item[0])

    return frames


def find_missing_frame_numbers(
    frame_numbers: Iterable[int],
) -> list[int]:
    """
    Find gaps in a zero-based trick-play frame sequence.

    Example:

        [0, 1, 2, 4]

    Returns:

        [3]
    """

    numbers = sorted(set(frame_numbers))

    if not numbers:
        return []

    highest = numbers[-1]

    expected = set(
        range(
            highest + 1,
        )
    )

    actual = set(numbers)

    return sorted(expected - actual)


# ============================================================================
# TRICK-PLAY VALIDATION
# ============================================================================


def validate_trickplay(
    file_id: str,
) -> TrickplayValidationResult:
    """
    Validate the complete trick-play cache for one video.
    """

    cache_directory = get_trickplay_directory(file_id)

    result = TrickplayValidationResult(
        file_id=file_id,
        valid=False,
        cache_directory=cache_directory,
    )

    if not cache_directory:
        result.reason = "Could not determine trick-play cache directory."

        return result

    if not os.path.isdir(cache_directory):
        result.reason = "Trick-play cache directory does not exist."

        return result

    result.directory_exists = True

    frame_files = get_trickplay_frame_files(cache_directory)

    result.frame_count = len(frame_files)

    if not frame_files:
        result.reason = "No trick-play frames were found."

        return result

    frame_numbers: list[int] = []

    for (
        frame_number,
        frame_path,
    ) in frame_files:
        frame_numbers.append(frame_number)

        frame_result = validate_jpeg_file(frame_path)

        result.frame_results.append(frame_result)

        if frame_result.valid:
            result.valid_frame_count += 1

        else:
            result.invalid_frame_count += 1

            result.invalid_frames.append(frame_path)

    # ------------------------------------------------------------------------
    # Verify frame numbering.
    # ------------------------------------------------------------------------

    result.missing_frame_numbers = find_missing_frame_numbers(frame_numbers)

    if (
        frame_numbers
        and frame_numbers[0] != 0
        and 0 not in result.missing_frame_numbers
    ):
        result.missing_frame_numbers.insert(
            0,
            0,
        )

    if result.invalid_frame_count > 0:
        result.reason = "One or more trick-play frames are invalid."

        return result

    if result.missing_frame_numbers:
        result.reason = "One or more trick-play frame numbers are missing."

        return result

    result.valid = True

    result.reason = "Trick-play cache is valid."

    return result


def remove_trickplay(
    file_id: str,
) -> bool:
    """
    Remove the complete trick-play cache for one video.
    """

    cache_directory = get_trickplay_directory(file_id)

    if not cache_directory:
        return False

    return safe_remove_directory(cache_directory)


def repair_trickplay(
    file_id: str,
    file_path: str,
) -> bool:
    """
    Repair trick-play assets for one video.

    Returns True only when the resulting cache validates successfully.
    """

    if not file_id:
        return False

    if not file_path:
        return False

    if not os.path.isfile(file_path):
        return False

    validation = validate_trickplay(file_id)

    if validation.valid:
        return True

    cache_directory = get_trickplay_directory(file_id)

    if cache_directory:
        if os.path.isdir(cache_directory):
            safe_remove_directory(cache_directory)

        try:
            os.makedirs(
                cache_directory,
                exist_ok=True,
            )

        except OSError as ex:
            log(
                f"<!> Could not create "
                f"trick-play cache directory "
                f"for {file_id}: "
                f"{type(ex).__name__}: {ex}"
            )

            return False

    try:
        trickplay_service.generate_trickplay(
            file_id,
            file_path,
        )

    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
    ) as ex:
        log(
            f"<!> Trick-play repair failed for "
            f"{os.path.basename(file_path)}: "
            f"{type(ex).__name__}: {ex}"
        )

        return False

    validation = validate_trickplay(file_id)

    return validation.valid


# ============================================================================
# SINGLE VIDEO MAINTENANCE
# ============================================================================


def validate_video_assets(
    file_id: str,
    validate_trickplay_assets: bool = True,
) -> VideoMaintenanceResult:
    """
    Validate all cached media assets for one indexed video.
    """

    result = VideoMaintenanceResult(
        file_id=file_id,
    )

    file_path = get_indexed_video_path(file_id)

    result.file_path = file_path

    if not file_path:
        result.errors.append("Video is not indexed.")

        return result

    if not os.path.isfile(file_path):
        result.errors.append("Source video file does not exist.")

        return result

    result.video_exists = True

    result.thumbnail = validate_thumbnail(file_path)

    if validate_trickplay_assets:
        result.trickplay = validate_trickplay(file_id)

    return result


def repair_video_assets(
    file_id: str,
    repair_thumbnail_asset: bool = True,
    repair_trickplay_assets: bool = False,
) -> VideoMaintenanceResult:
    """
    Validate and optionally repair cached assets for one video.
    """

    result = validate_video_assets(
        file_id=file_id,
        validate_trickplay_assets=(repair_trickplay_assets),
    )

    if not result.video_exists:
        return result

    file_path = result.file_path

    if not file_path:
        return result

    # ------------------------------------------------------------------------
    # Thumbnail repair.
    # ------------------------------------------------------------------------

    if (
        repair_thumbnail_asset
        and result.thumbnail is not None
        and not result.thumbnail.valid
    ):
        repaired = repair_thumbnail(file_path)

        result.thumbnail_repaired = repaired

        result.thumbnail = validate_thumbnail(file_path)

    # ------------------------------------------------------------------------
    # Trick-play repair.
    # ------------------------------------------------------------------------

    if (
        repair_trickplay_assets
        and result.trickplay is not None
        and not result.trickplay.valid
    ):
        repaired = repair_trickplay(
            file_id=file_id,
            file_path=file_path,
        )

        result.trickplay_repaired = repaired

        result.trickplay = validate_trickplay(file_id)

    return result


# ============================================================================
# CATALOG REPORT HELPERS
# ============================================================================


def update_thumbnail_report(
    report: MaintenanceReport,
    thumbnail: AssetValidationResult,
) -> None:
    """
    Update aggregate thumbnail statistics.
    """

    if thumbnail.valid:
        report.thumbnails_valid += 1

        return

    if not thumbnail.exists:
        report.thumbnails_missing += 1

        return

    if thumbnail.empty:
        report.thumbnails_empty += 1

        return

    if thumbnail.corrupt:
        report.thumbnails_corrupt += 1

        return

    if thumbnail.stale:
        report.thumbnails_stale += 1

        return

    report.thumbnails_corrupt += 1


def update_trickplay_report(
    report: MaintenanceReport,
    trickplay: TrickplayValidationResult,
) -> None:
    """
    Update aggregate trick-play statistics.
    """

    if trickplay.valid:
        report.trickplay_valid += 1

        return

    if not trickplay.directory_exists:
        report.trickplay_missing += 1

        return

    report.trickplay_invalid += 1


# ============================================================================
# CATALOG-WIDE VALIDATION
# ============================================================================


def scan_catalog(
    validate_trickplay_assets: bool = False,
    repair_thumbnails: bool = False,
    repair_trickplay_assets: bool = False,
    include_video_results: bool = False,
) -> MaintenanceReport:
    """
    Scan the complete indexed video catalog.
    """

    report = MaintenanceReport(started_at=time.time())

    catalog_items = get_catalog_snapshot()

    log(f"--> Maintenance scan started: {len(catalog_items)} catalog items.")

    for item in catalog_items:
        file_id = get_file_id_from_item(item)

        file_path = get_file_path_from_item(item)

        if not file_id:
            report.errors.append("Catalog item is missing a file ID.")

            report.videos_with_errors += 1

            continue

        report.videos_scanned += 1

        # --------------------------------------------------------------------
        # Missing source video.
        # --------------------------------------------------------------------

        if not file_path or not os.path.isfile(file_path):
            report.videos_missing += 1

            report.videos_with_errors += 1

            video_result = VideoMaintenanceResult(
                file_id=file_id,
                file_path=file_path or None,
                video_exists=False,
                errors=["Source video file does not exist."],
            )

            if include_video_results:
                report.video_results.append(video_result)

            continue

        # --------------------------------------------------------------------
        # Validate assets.
        # --------------------------------------------------------------------

        video_result = validate_video_assets(
            file_id=file_id,
            validate_trickplay_assets=(
                validate_trickplay_assets or repair_trickplay_assets
            ),
        )

        thumbnail = video_result.thumbnail

        if thumbnail is not None:
            update_thumbnail_report(
                report,
                thumbnail,
            )

        trickplay = video_result.trickplay

        if trickplay is not None:
            update_trickplay_report(
                report,
                trickplay,
            )

        # --------------------------------------------------------------------
        # Thumbnail repair.
        # --------------------------------------------------------------------

        if (
            repair_thumbnails
            and thumbnail is not None
            and not thumbnail.valid
            and repair_thumbnail(file_path)
        ):
            report.thumbnails_repaired += 1
            video_result.thumbnail_repaired = True
            video_result.thumbnail = validate_thumbnail(file_path)

        # --------------------------------------------------------------------
        # Trick-play repair.
        # --------------------------------------------------------------------

        if (
            repair_trickplay_assets
            and trickplay is not None
            and not trickplay.valid
            and repair_trickplay(file_id=file_id, file_path=file_path)
        ):
            report.trickplay_repaired += 1
            video_result.trickplay_repaired = True
            video_result.trickplay = validate_trickplay(file_id)

        # --------------------------------------------------------------------
        # Error count.
        # --------------------------------------------------------------------

        has_error = (
            video_result.errors
            or (video_result.thumbnail is not None and not video_result.thumbnail.valid)
            or (
                (validate_trickplay_assets or repair_trickplay_assets)
                and video_result.trickplay is not None
                and not video_result.trickplay.valid
            )
        )

        if has_error:
            report.videos_with_errors += 1

        if include_video_results:
            report.video_results.append(video_result)

    report.complete()

    log(f"--> Maintenance scan complete: {report.videos_scanned} videos scanned.")

    return report


# ============================================================================
# ORPHANED THUMBNAIL CLEANUP
# ============================================================================


def get_thumbnail_cache_directory() -> str | None:
    """
    Determine the most likely shared thumbnail cache directory.

    The path is derived from indexed video thumbnail paths.
    """

    parents: list[str] = []

    for item in get_catalog_snapshot():
        file_path = get_file_path_from_item(item)

        if not file_path:
            continue

        thumbnail_path = get_thumbnail_path(file_path)

        if not thumbnail_path:
            continue

        parents.append(os.path.dirname(thumbnail_path))

    if not parents:
        return None

    counts: dict[str, int] = {}

    for parent in parents:
        counts[parent] = (
            counts.get(
                parent,
                0,
            )
            + 1
        )

    # Explicit lambda avoids the Pylance overload problem with:
    #
    #     key=counts.get
    #
    return max(
        counts,
        key=lambda key: counts[key],
    )


def get_expected_thumbnail_paths() -> set[str]:
    """
    Return all thumbnail paths expected for currently indexed videos.
    """

    expected_paths: set[str] = set()

    for item in get_catalog_snapshot():
        file_path = get_file_path_from_item(item)

        if not file_path:
            continue

        thumbnail_path = get_thumbnail_path(file_path)

        if thumbnail_path:
            expected_paths.add(os.path.abspath(thumbnail_path))

    return expected_paths


def find_orphaned_thumbnails() -> list[str]:
    """
    Find thumbnail files that do not belong to an indexed video.
    """

    cache_directory = get_thumbnail_cache_directory()

    if not cache_directory:
        return []

    if not os.path.isdir(cache_directory):
        return []

    expected_paths = get_expected_thumbnail_paths()

    orphaned: list[str] = []

    try:
        names = os.listdir(cache_directory)

    except OSError:
        return []

    for name in names:
        if not name.lower().endswith(".jpg"):
            continue

        path = os.path.abspath(
            os.path.join(
                cache_directory,
                name,
            )
        )

        if not os.path.isfile(path):
            continue

        if path not in expected_paths:
            orphaned.append(path)

    return orphaned


def cleanup_orphaned_thumbnails(
    dry_run: bool = True,
) -> list[str]:
    """
    Find and optionally remove orphaned thumbnail files.

    dry_run=True:
        Only return files that would be removed.

    dry_run=False:
        Remove the orphaned files.
    """

    orphaned = find_orphaned_thumbnails()

    if dry_run:
        return orphaned

    removed: list[str] = []

    for path in orphaned:
        if safe_remove_file(path):
            removed.append(path)

    return removed


# ============================================================================
# ORPHANED TRICK-PLAY CLEANUP
# ============================================================================


def get_trickplay_root_directory() -> str | None:
    """
    Determine the most likely shared trick-play cache root directory.

    Trick-play directories are derived from the indexed file IDs.
    """

    parents: list[str] = []

    for file_id in get_all_indexed_file_ids():
        directory = get_trickplay_directory(file_id)

        if directory:
            parents.append(os.path.dirname(directory))

    if not parents:
        return None

    counts: dict[str, int] = {}

    for parent in parents:
        counts[parent] = (
            counts.get(
                parent,
                0,
            )
            + 1
        )

    # IMPORTANT:
    #
    # This return MUST remain inside get_trickplay_root_directory().
    #
    # The previous error occurred because it was accidentally placed at
    # module level, causing:
    #
    #     "return can be used only within a function"
    #
    return max(
        counts,
        key=lambda key: counts[key],
    )


def find_orphaned_trickplay_directories() -> list[str]:
    """
    Find trick-play directories whose names do not correspond to indexed IDs.
    """

    root_directory = get_trickplay_root_directory()

    if not root_directory:
        return []

    if not os.path.isdir(root_directory):
        return []

    indexed_ids = get_all_indexed_file_ids()

    orphaned: list[str] = []

    try:
        names = os.listdir(root_directory)

    except OSError:
        return []

    for name in names:
        path = os.path.join(
            root_directory,
            name,
        )

        if not os.path.isdir(path):
            continue

        if name not in indexed_ids:
            orphaned.append(path)

    return orphaned


def cleanup_orphaned_trickplay(
    dry_run: bool = True,
) -> list[str]:
    """
    Find and optionally remove orphaned trick-play directories.
    """

    orphaned = find_orphaned_trickplay_directories()

    if dry_run:
        return orphaned

    removed: list[str] = []

    for path in orphaned:
        if safe_remove_directory(path):
            removed.append(path)

    return removed


# ============================================================================
# COMPLETE CACHE MAINTENANCE
# ============================================================================


def run_cache_maintenance(
    validate_trickplay_assets: bool = False,
    repair_thumbnails: bool = False,
    repair_trickplay_assets: bool = False,
    cleanup_orphans: bool = False,
    include_video_results: bool = False,
) -> MaintenanceReport:
    """
    Run complete cache maintenance.

    Default behavior:

        - Validate thumbnails.
        - Do not validate trick-play unless requested.
        - Do not repair anything.
        - Do not delete anything.
    """

    report = scan_catalog(
        validate_trickplay_assets=(
            validate_trickplay_assets or repair_trickplay_assets
        ),
        repair_thumbnails=(repair_thumbnails),
        repair_trickplay_assets=(repair_trickplay_assets),
        include_video_results=(include_video_results),
    )

    # ------------------------------------------------------------------------
    # Orphan detection.
    # ------------------------------------------------------------------------

    orphaned_thumbnails = find_orphaned_thumbnails()

    orphaned_trickplay = find_orphaned_trickplay_directories()

    report.orphaned_thumbnail_files = len(orphaned_thumbnails)

    report.orphaned_trickplay_directories = len(orphaned_trickplay)

    # ------------------------------------------------------------------------
    # Optional orphan cleanup.
    # ------------------------------------------------------------------------

    if cleanup_orphans:
        removed_thumbnails = cleanup_orphaned_thumbnails(dry_run=False)

        removed_trickplay = cleanup_orphaned_trickplay(dry_run=False)

        log(f"--> Removed {len(removed_thumbnails)} orphaned thumbnails.")

        log(f"--> Removed {len(removed_trickplay)} orphaned trick-play directories.")

    return report


# ============================================================================
# SIMPLE PUBLIC ROUTINES
# ============================================================================


def validate_all_thumbnails() -> MaintenanceReport:
    """
    Validate all indexed thumbnails.

    No files are modified.
    """

    return run_cache_maintenance(
        validate_trickplay_assets=False,
        repair_thumbnails=False,
        repair_trickplay_assets=False,
        cleanup_orphans=False,
    )


def repair_all_thumbnails() -> MaintenanceReport:
    """
    Validate and repair all indexed thumbnails.

    Trick-play assets are not touched.
    """

    return run_cache_maintenance(
        validate_trickplay_assets=False,
        repair_thumbnails=True,
        repair_trickplay_assets=False,
        cleanup_orphans=False,
    )


def validate_all_trickplay() -> MaintenanceReport:
    """
    Validate trick-play caches for all indexed videos.

    No files are modified.
    """

    return run_cache_maintenance(
        validate_trickplay_assets=True,
        repair_thumbnails=False,
        repair_trickplay_assets=False,
        cleanup_orphans=False,
    )


def repair_all_trickplay() -> MaintenanceReport:
    """
    Validate and repair trick-play caches for all indexed videos.
    """

    return run_cache_maintenance(
        validate_trickplay_assets=True,
        repair_thumbnails=False,
        repair_trickplay_assets=True,
        cleanup_orphans=False,
    )


def cleanup_all_orphans(
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Find or remove all orphaned cache assets.

    dry_run=True:
        Nothing is deleted.

    dry_run=False:
        Orphaned thumbnail files and trick-play directories are removed.
    """

    thumbnails = cleanup_orphaned_thumbnails(dry_run=dry_run)

    trickplay = cleanup_orphaned_trickplay(dry_run=dry_run)

    return {
        "success": True,
        "dryRun": dry_run,
        "thumbnailFiles": thumbnails,
        "trickplayDirectories": trickplay,
        "thumbnailCount": len(thumbnails),
        "trickplayCount": len(trickplay),
    }


# ============================================================================
# END OF FILE
# ============================================================================

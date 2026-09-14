from fastapi.responses import JSONResponse
from fastapi import HTTPException, status

import services.trickplay_service as trickplay_service
import tools.maintenance_routines as maintenance_routines
from config import log
from services.video_service import ensure_directory_indexed

import config
from services.drive_service import get_authorized_drives
from services.video_service import ensure_directory_indexed


def handle_reindex(request, body: dict) -> JSONResponse:
    """Reindex specified drives or all authorized drives when omitted.

    Body format:
      { "drives": ["Vids", "Vids2"] }

    If `drives` is omitted or empty, the server will reindex all drives
    listed in `authorized_drives.json`.
    """
    del request

    requested = body.get("drives") if isinstance(body, dict) else None

    if not requested:
        requested = sorted(list(get_authorized_drives()))

    if not requested:
        return JSONResponse(content={"success": True, "results": [], "message": "No drives to reindex."})

    results = []

    for drive_name in requested:
        try:
            reindexed = ensure_directory_indexed(drive_name, None)
            results.append({"drive": drive_name, "reindexed": bool(reindexed)})
        except Exception as ex:
            results.append({"drive": drive_name, "reindexed": False, "error": str(ex)})

    return JSONResponse(content={"success": True, "results": results})


def handle_validate_all_thumbnails(request) -> JSONResponse:
    result = maintenance_routines.validate_all_thumbnails()
    return JSONResponse(content=result.to_dict())


def handle_repair_all_thumbnails(request) -> JSONResponse:
    result = maintenance_routines.repair_all_thumbnails()
    return JSONResponse(content=result.to_dict())


def handle_validate_all_trickplay(request) -> JSONResponse:
    result = maintenance_routines.validate_all_trickplay()
    return JSONResponse(content=result.to_dict())


def handle_repair_all_trickplay(request) -> JSONResponse:
    result = maintenance_routines.repair_all_trickplay()
    return JSONResponse(content=result.to_dict())


async def handle_create_missing_trickplay_folders(request) -> JSONResponse:
    try:
        result = await trickplay_service.create_missing_trickplay_folders()

    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        Exception,
    ) as ex:
        log(f"<!> Missing TrickPlay maintenance failed: {type(ex).__name__}: {ex}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "success": False,
                "message": "Missing TrickPlay folder maintenance failed.",
                "errorType": type(ex).__name__,
                "error": str(ex),
            },
        ) from ex

    result_dict = result if isinstance(result, dict) else getattr(result, "to_dict", lambda: result)()

    if isinstance(result_dict, dict) and not result_dict.get("success", False):
        log("<!> Missing TrickPlay maintenance returned a failure result.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result_dict,
        )

    return JSONResponse(content=result_dict)


def handle_inspect_orphans(request) -> JSONResponse:
    result = maintenance_routines.cleanup_all_orphans(dry_run=True)
    return JSONResponse(content=result)


def handle_cleanup_orphans(request) -> JSONResponse:
    result = maintenance_routines.cleanup_all_orphans(dry_run=False)
    return JSONResponse(content=result)


def handle_validate_video_assets(request, file_id: str, validate_trickplay_assets: bool = True) -> JSONResponse:
    result = maintenance_routines.validate_video_assets(
        file_id, validate_trickplay_assets=validate_trickplay_assets
    )
    return JSONResponse(content=maintenance_routines.video_maintenance_result_to_dict(result))


def handle_repair_video_assets(request, file_id: str, repair_thumbnail_asset: bool = True, repair_trickplay_assets: bool = False) -> JSONResponse:
    result = maintenance_routines.repair_video_assets(
        file_id,
        repair_thumbnail_asset=repair_thumbnail_asset,
        repair_trickplay_assets=repair_trickplay_assets,
    )
    return JSONResponse(content=maintenance_routines.video_maintenance_result_to_dict(result))


def handle_run_maintenance(request, validate_trickplay_assets: bool = False, repair_thumbnails: bool = False, repair_trickplay_assets: bool = False, cleanup_orphans: bool = False, include_video_results: bool = False) -> JSONResponse:
    result = maintenance_routines.run_cache_maintenance(
        validate_trickplay_assets=validate_trickplay_assets,
        repair_thumbnails=repair_thumbnails,
        repair_trickplay_assets=repair_trickplay_assets,
        cleanup_orphans=cleanup_orphans,
        include_video_results=include_video_results,
    )

    return JSONResponse(content=result.to_dict(include_video_results))


def handle_reindex_directory(request, body: dict) -> JSONResponse:
    """Reindex a specific directory on a drive.

    Body expected: { "drive": "Vids", "directory": "0Q" }

    The `directory` value may be empty or None to reindex the drive root.
    """
    del request

    if not isinstance(body, dict):
        return JSONResponse(content={"success": False, "error": "Invalid payload"}, status_code=400)

    drive = str(body.get("drive") or "").strip()
    directory = body.get("directory")

    if not drive:
        return JSONResponse(content={"success": False, "error": "drive is required"}, status_code=400)

    try:
        reindexed = ensure_directory_indexed(drive, directory)

        return JSONResponse(
            content={
                "success": True,
                "drive": drive,
                "directory": directory or "",
                "reindexed": bool(reindexed),
            }
        )

    except Exception as ex:
        log(f"<!> Reindex directory failed for {drive}/{directory}: {type(ex).__name__}: {ex}")

        return JSONResponse(
            content={
                "success": False,
                "drive": drive,
                "directory": directory or "",
                "error": str(ex),
            },
            status_code=500,
        )

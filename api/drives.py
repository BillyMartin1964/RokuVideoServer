from services.drive_service import get_system_drives


def handle_get_drives(request=None):
    drives = get_system_drives()
    return {
        "success": True,
        "total": len(drives),
        "data": drives,
    }
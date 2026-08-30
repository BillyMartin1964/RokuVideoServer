from pydantic import BaseModel, Field


def normalize_directory_path(path: str) -> str:
    """Normalizes a directory string to standard format: leading slash, no trailing slash.

    Examples:
        "" or "/"           -> ""
        "Fav" or "/Fav/"    -> "/Fav"
        "/Fav/AndiJames/"   -> "/Fav/AndiJames"
    """
    if not path:
        return ""
    cleaned = f"/{path.strip('/')}"
    return "" if cleaned == "/" else cleaned


class VideoModel(BaseModel):
    """Complete data model representing a video in the media catalog.

    This model contains metadata and URLs describing the video.

    It does NOT contain:
        - actual video bytes
        - actual thumbnail image bytes

    thumbnailUrl points to the thumbnail endpoint.

    streamUrl points to the video streaming endpoint.
    """

    # ------------------------------------------------------------------------
    # Core identity
    # ------------------------------------------------------------------------

    id: str = ""

    fileId: str = ""

    title: str = "Untitled"

    name: str = ""

    fileName: str = ""

    ext: str = ""

    # ------------------------------------------------------------------------
    # Location
    # ------------------------------------------------------------------------

    drive: str = ""

    directory: str = ""

    fullPath: str = ""

    # ------------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------------

    streamUrl: str = ""

    thumbnailUrl: str = ""

    # ------------------------------------------------------------------------
    # Video metadata
    # ------------------------------------------------------------------------

    description: str = ""

    duration: int = 0

    width: int = 0

    height: int = 0

    releaseDate: str = ""

    # ------------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------------

    categories: list[str] = Field(default_factory=list)

    # ------------------------------------------------------------------------
    # Playback state
    # ------------------------------------------------------------------------

    bookmarkPosition: int = 0


def create_video_model(
    data: dict,
    base_url: str = "",
) -> VideoModel:
    """Convert catalog data into a complete VideoModel.

    The input may come from:
        - the video catalog
        - the disk cache
        - a filesystem scan
        - an existing API response

    This function normalizes the different field names used by the
    catalog into the VideoModel contract.

    It does not generate thumbnails and does not stream video.

    It only creates the model and assigns the URLs that point to those
    separate services.
    """

    if not isinstance(data, dict):
        return VideoModel()

    # ------------------------------------------------------------------------
    # Normalize base URL
    # ------------------------------------------------------------------------

    clean_base = str(base_url or "").rstrip("/")

    # ------------------------------------------------------------------------
    # ID
    # ------------------------------------------------------------------------

    raw_id = str(data.get("id") or data.get("fileId") or "")

    # ------------------------------------------------------------------------
    # Name
    # ------------------------------------------------------------------------

    raw_name = str(data.get("name") or "")

    # ------------------------------------------------------------------------
    # Path
    # ------------------------------------------------------------------------

    raw_path = str(data.get("path") or data.get("fullPath") or "")

    # ------------------------------------------------------------------------
    # Extension
    # ------------------------------------------------------------------------

    raw_ext = str(data.get("ext") or data.get("extension") or "")

    if not raw_ext and raw_path and "." in raw_path:
        raw_ext = raw_path.rsplit(".", 1)[-1]

    if raw_ext and not raw_ext.startswith("."):
        raw_ext = "." + raw_ext

    # ------------------------------------------------------------------------
    # Title
    # ------------------------------------------------------------------------

    raw_title = str(data.get("title") or raw_name or "Untitled")

    # ------------------------------------------------------------------------
    # File name
    # ------------------------------------------------------------------------

    supplied_file_name = str(data.get("fileName") or "")

    if supplied_file_name:
        file_name = supplied_file_name
    elif raw_name:
        file_name = f"{raw_name}{raw_ext}" if raw_ext else raw_name
    else:
        file_name = ""

    # ------------------------------------------------------------------------
    # Drive
    # ------------------------------------------------------------------------

    drive = str(data.get("drive") or "")

    # ------------------------------------------------------------------------
    # Directory (Standardized and normalized path)
    # ------------------------------------------------------------------------

    raw_directory = str(data.get("directory") or data.get("subfolder") or "")
    directory = normalize_directory_path(raw_directory)

    # ------------------------------------------------------------------------
    # Description
    # ------------------------------------------------------------------------

    description = str(data.get("description") or "")

    # ------------------------------------------------------------------------
    # Duration
    # ------------------------------------------------------------------------

    duration_value = data.get("duration") or 0

    try:
        duration = int(duration_value)
    except (TypeError, ValueError):
        duration = 0

    # ------------------------------------------------------------------------
    # Width
    # ------------------------------------------------------------------------

    width_value = data.get("width") or 0

    try:
        width = int(width_value)
    except (TypeError, ValueError):
        width = 0

    # ------------------------------------------------------------------------
    # Height
    # ------------------------------------------------------------------------

    height_value = data.get("height") or 0

    try:
        height = int(height_value)
    except (TypeError, ValueError):
        height = 0

    # ------------------------------------------------------------------------
    # Release date
    # ------------------------------------------------------------------------

    release_date = str(data.get("releaseDate") or "")

    # ------------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------------

    categories_value = data.get("categories") or []

    if isinstance(categories_value, list):
        categories = [str(category) for category in categories_value]
    else:
        categories = []

    # ------------------------------------------------------------------------
    # Bookmark position
    # ------------------------------------------------------------------------

    bookmark_value = data.get("bookmarkPosition") or 0

    try:
        bookmark_position = int(bookmark_value)
    except (TypeError, ValueError):
        bookmark_position = 0

    # ------------------------------------------------------------------------
    # Thumbnail URL
    #
    # The model contains the URL.
    #
    # The thumbnail endpoint returns the actual JPEG later.
    # ------------------------------------------------------------------------

    poster_url = str(
        data.get("thumbnailUrl") or data.get("thumbnail") or data.get("poster") or ""
    )

    if raw_id and clean_base and not poster_url:
        poster_url = f"{clean_base}/api/thumbnails/{raw_id}"

    # ------------------------------------------------------------------------
    # Stream URL
    #
    # The model contains the URL.
    #
    # The streaming endpoint returns the actual video bytes later.
    # ------------------------------------------------------------------------

    stream_url = str(data.get("streamUrl") or data.get("url") or "")

    if raw_id and clean_base:
        stream_url = f"{clean_base}/api/videos/{raw_id}"

    # ------------------------------------------------------------------------
    # Create the actual Pydantic model
    # ------------------------------------------------------------------------

    return VideoModel(
        id=raw_id,
        fileId=raw_id,
        title=raw_title,
        name=raw_name,
        fileName=file_name,
        ext=raw_ext,
        drive=drive,
        directory=directory,
        fullPath=raw_path,
        streamUrl=stream_url,
        description=description,
        duration=duration,
        height=height,
        width=width,
        thumbnailUrl=poster_url,
        releaseDate=release_date,
        categories=categories,
        bookmarkPosition=bookmark_position,
    )
from pydantic import BaseModel, Field


class VideoModel(BaseModel):
    # Core identifiers & file paths
    id: str = ""
    fileId: str = ""
    title: str = "Untitled"
    name: str = ""
    ext: str = ""
    drive: str = ""
    directory: str = ""
    fullPath: str = ""
    streamUrl: str = ""
    fileName: str = ""

    # Playback & visual metadata
    description: str = ""
    duration: int = 0
    height: int = 0
    width: int = 0
    hdPosterUrl: str = ""
    posterUrl: str = ""
    releaseDate: str = ""
    categories: list[str] = Field(default_factory=list)
    bookmarkPosition: int = 0


def create_video_model(data: dict, base_url: str = "") -> VideoModel:
    """
    Standard function to map raw dictionary data into a clean VideoModel.
    Normalizes fallback keys safely using standard Python dict gets.
    """
    if not isinstance(data, dict):
        return VideoModel()

    raw_id = str(data.get("id", ""))
    raw_name = str(data.get("name", ""))
    raw_path = str(data.get("path") or data.get("fullPath", ""))
    raw_ext = str(data.get("ext", ""))

    # Infer extension if missing
    if not raw_ext and raw_path and "." in raw_path:
        raw_ext = raw_path.rsplit(".", 1)[-1]

    # Standardize title
    raw_title = str(data.get("title") or raw_name or "Untitled")

    # Standardize fileName
    file_name = f"{raw_name}.{raw_ext}" if raw_ext else raw_name

    # Construct stream URL
    stream_url = str(data.get("url") or data.get("streamUrl", ""))
    if not stream_url and raw_id:
        clean_base = base_url.rstrip("/") if base_url else ""
        stream_url = f"{clean_base}/api/stream/{raw_id}"

    return VideoModel(
        id=raw_id,
        fileId=raw_id,
        title=raw_title,
        name=raw_name,
        ext=raw_ext,
        drive=str(data.get("drive", "")),
        directory=str(data.get("directory") or data.get("subfolder", "")),
        fullPath=raw_path,
        streamUrl=stream_url,
        fileName=file_name,
        description=str(data.get("description", "")),
        duration=int(data.get("duration") or 0),
        height=int(data.get("height") or 0),
        width=int(data.get("width") or 0),
        hdPosterUrl=str(data.get("hdPosterUrl") or data.get("poster", "")),
        posterUrl=str(data.get("posterUrl") or data.get("thumbnail", "")),
        releaseDate=str(data.get("releaseDate", "")),
        categories=list(data.get("categories") or []),
        bookmarkPosition=int(data.get("bookmarkPosition") or 0),
    )

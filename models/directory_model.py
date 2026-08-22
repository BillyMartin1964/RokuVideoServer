from pydantic import BaseModel, Field


class DirectoryModel(BaseModel):
    drive: str = ""
    subfolder: str = ""
    dirKey: str = ""
    name: str = ""
    title: str = ""
    thumbUrl: str = ""

    # Complete directory hierarchy.
    # Example:
    # ["Movies", "Action", "Marvel", "2025"]
    path: list[str] = Field(default_factory=list)

    # Number of directory levels in path.
    depth: int = 0

    # Immediate parent directory name.
    parent: str = ""

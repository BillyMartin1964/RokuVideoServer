from pydantic import BaseModel, Field, computed_field


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


class DirectoryModel(BaseModel):
    drive: str = ""
    directory: str = ""  # Standardized relative path (e.g., "/Fav/AndiJames")
    dirKey: str = ""  # Format: "{drive}|{directory}"
    name: str = ""  # Formatted label (e.g., "Vids / Fav/AndiJames")
    title: str = ""  # Folder display name (e.g., "AndiJames")
    thumbUrl: str = ""

    path: list[str] = Field(
        default_factory=list
    )  # Hierarchy array (e.g., ["Fav", "AndiJames"])
    depth: int = 0  # 1 for root, len(path) for subfolders
    parent: str = ""  # Name of immediate parent folder or drive
    isFolder: bool = True  # Always True for directory models
    childCount: int = 0  # Number of immediate child directories

    @computed_field
    @property
    def subfolder(self) -> str:
        """Alias property for backward compatibility with clients expecting 'subfolder'."""
        return self.directory

    @classmethod
    def create(
        cls,
        drive: str,
        directory: str = "",
        thumb_url: str = "",
        child_count: int = 0,
    ) -> "DirectoryModel":
        """Factory constructor that derives all standardized hierarchy fields."""
        norm_dir = normalize_directory_path(directory)
        path_parts = [part for part in norm_dir.split("/") if part]

        if path_parts:
            title = path_parts[-1]
            parent = path_parts[-2] if len(path_parts) > 1 else drive
            depth = len(path_parts)
            name = f"{drive} / {norm_dir.lstrip('/')}"
        else:
            title = drive
            parent = ""
            depth = 1
            name = drive

        dir_key = f"{drive}|{norm_dir}"

        return cls(
            drive=drive,
            directory=norm_dir,
            dirKey=dir_key,
            name=name,
            title=title,
            thumbUrl=thumb_url,
            path=path_parts,
            depth=depth,
            parent=parent,
            isFolder=True,
            childCount=child_count,
        )
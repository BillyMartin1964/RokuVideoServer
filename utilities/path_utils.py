def normalize_path_value(value) -> str:
    """
    Normalize a path-like value for comparison.

    This function is intended for comparing API/catalog values such as
    drive names, directory paths, and subfolder paths.

    The following values are considered equivalent:

        Fav
        /Fav
        Fav/
        /Fav/

    Backslashes are converted to forward slashes.

    Examples:

        Vids       -> Vids
        /Vids      -> Vids
        Vids/      -> Vids
        /Vids/     -> Vids

        /Movies/Favorites/
        -> Movies/Favorites

    This function does NOT modify physical filesystem paths.
    It is intended only for normalization before comparison.
    """

    if value is None:
        return ""

    normalized = str(value).strip()

    if not normalized:
        return ""

    normalized = normalized.replace("\\", "/")

    normalized = normalized.strip("/")

    return normalized

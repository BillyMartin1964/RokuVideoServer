"""
Search model for video filename/title searches.

This module contains the data model used to represent a video search request.

The model intentionally contains no FastAPI, catalog, filesystem, or
VideoModel dependencies.

The model represents:

    - The original search text.
    - Terms that must be present.
    - Terms that must not be present.
    - The field to search.
    - An optional drive filter.
    - An optional directory filter.
    - Result offset.
    - Result limit.

The model is intentionally independent of the API and search implementation.
It can therefore be created at the API boundary and passed through the
application as a single search request object.

Examples:

    "deer bear"
        include_terms = ["deer", "bear"]

    "deer -dog"
        include_terms = ["deer"]
        exclude_terms = ["dog"]

    "-dog deer bear"
        include_terms = ["deer", "bear"]
        exclude_terms = ["dog"]
"""

from dataclasses import dataclass, field
from enum import Enum

# ============================================================================
# SEARCH FIELD
# ============================================================================


class VideoSearchField(str, Enum):
    """
    Identifies which VideoModel value should be searched.
    """

    TITLE = "title"
    FILENAME = "fileName"
    TITLE_AND_FILENAME = "title_and_filename"


# ============================================================================
# VIDEO SEARCH MODEL
# ============================================================================


@dataclass(slots=True)
class VideoSearchModel:
    """
    Represents a complete video search request.

    Attributes:
        search_text:
            The original search text supplied by the caller.

        include_terms:
            Terms that must be present for a video to match.

        exclude_terms:
            Terms that must not be present for a video to match.

        field:
            Determines whether the title, filename, or both are searched.

        drive:
            Optional drive name used to restrict the search.

        directory:
            Optional directory or subfolder used to restrict the search.

        offset:
            Number of matching results to skip.

        limit:
            Maximum number of matching results to return.
            A value of 0 means all remaining matches.
    """

    search_text: str = ""
    include_terms: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    field: VideoSearchField = VideoSearchField.FILENAME
    drive: str | None = None
    directory: str | None = None
    offset: int = 0
    limit: int = 0

    # ========================================================================
    # FACTORY
    # ========================================================================

    @classmethod
    def create(
        cls,
        search_text: str | None = None,
        include_terms: list[str] | None = None,
        exclude_terms: list[str] | None = None,
        field: VideoSearchField = VideoSearchField.FILENAME,
        drive: str | None = None,
        directory: str | None = None,
        offset: int = 0,
        limit: int = 0,
    ) -> "VideoSearchModel":
        """
        Create a VideoSearchModel from supplied search values.

        The include and exclude lists are copied so that the caller's lists
        cannot be modified through the model.

        Pagination values are normalized to the API limits.

        Args:
            search_text:
                Original user-entered search text.

            include_terms:
                Terms that must be present.

            exclude_terms:
                Terms that must not be present.

            field:
                Field to search.

            drive:
                Optional drive filter.

            directory:
                Optional directory filter.

            offset:
                Number of matching results to skip.

            limit:
                Maximum number of results to return.
                Zero means all remaining results.

        Returns:
            A new VideoSearchModel.
        """

        model = cls(
            search_text=str(search_text or "").strip(),
            include_terms=list(include_terms or []),
            exclude_terms=list(exclude_terms or []),
            field=field,
            drive=(str(drive).strip() if drive is not None else None),
            directory=(str(directory).strip() if directory is not None else None),
            offset=max(0, offset),
            limit=max(0, min(limit, 500)),
        )

        model.normalize_terms()

        return model

    @classmethod
    def from_query_params(
        cls,
        file_name: str | None = None,
        search_field: str = "fileName",
        exclude_words: str | None = None,
        drive: str | None = None,
        directory: str | None = None,
        offset: int = 0,
        limit: int = 0,
    ) -> "VideoSearchModel":
        """
        Construct a VideoSearchModel directly from API route query parameters.
        """
        search_text = str(file_name or "").strip()

        norm_field = str(search_field or "").strip().lower()
        if norm_field == "title":
            target_field = VideoSearchField.TITLE
        elif norm_field in ("title_and_filename", "titleandfilename"):
            target_field = VideoSearchField.TITLE_AND_FILENAME
        else:
            target_field = VideoSearchField.FILENAME

        inc_terms: list[str] = []
        exc_terms: list[str] = []

        if search_text:
            tokens = search_text.split()
            for token in tokens:
                if token.startswith("-") and len(token) > 1:
                    exc_terms.append(token[1:])
                else:
                    inc_terms.append(token)

        if exclude_words:
            for word in str(exclude_words).replace(",", " ").split():
                cleaned = word.strip().lstrip("-")
                if cleaned:
                    exc_terms.append(cleaned)

        return cls.create(
            search_text=search_text,
            include_terms=inc_terms,
            exclude_terms=exc_terms,
            field=target_field,
            drive=drive,
            directory=directory,
            offset=offset,
            limit=limit,
        )

    # ========================================================================
    # PROPERTIES
    # ========================================================================

    @property
    def has_search_text(self) -> bool:
        """
        Return True when the model contains search criteria.
        """

        return bool(self.search_text or self.include_terms or self.exclude_terms)

    @property
    def has_include_terms(self) -> bool:
        """
        Return True when the model contains required search terms.
        """

        return bool(self.include_terms)

    @property
    def has_exclude_terms(self) -> bool:
        """
        Return True when the model contains excluded search terms.
        """

        return bool(self.exclude_terms)

    @property
    def is_title_search(self) -> bool:
        """
        Return True when the title should be searched.
        """

        return self.field in (
            VideoSearchField.TITLE,
            VideoSearchField.TITLE_AND_FILENAME,
        )

    @property
    def is_filename_search(self) -> bool:
        """
        Return True when the filename should be searched.
        """

        return self.field in (
            VideoSearchField.FILENAME,
            VideoSearchField.TITLE_AND_FILENAME,
        )

    # ========================================================================
    # NORMALIZATION
    # ========================================================================

    def normalize_terms(self) -> None:
        """
        Normalize the search text and search terms.

        This method performs only model-level cleanup.
        """

        self.search_text = self.search_text.strip()

        self.include_terms = [
            term.strip() for term in self.include_terms if term.strip()
        ]

        self.exclude_terms = [
            term.strip() for term in self.exclude_terms if term.strip()
        ]
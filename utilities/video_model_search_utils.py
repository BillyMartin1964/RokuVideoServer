# ============================================================================
# VIDEO_MODEL_SEARCH_UTILS.PY
# ============================================================================

import re
import unicodedata

"""
Utility functions for searching video filenames.

These functions intentionally contain no FastAPI, catalog, filesystem,
or VideoModel dependencies.

The API layer supplies a filename and a search string, and these
utilities determine whether the filename matches the search.

Search behavior:

    - Case insensitive.
    - Ignores common filename separators and punctuation.
    - Multiple search terms must all match.
    - Search terms may appear in any order.
    - Partial terms are supported.
    - Search text without spaces can match words separated by spaces
      or punctuation in the filename.

Examples:

    "deer"      -> "Deer and Bear in the Woods.mp4"   MATCH
    "deer bear" -> "Deer and Bear in the Woods.mp4"   MATCH
    "bear deer" -> "Deer and Bear in the Woods.mp4"   MATCH
    "mom"       -> "Mom and Son Playing.mp4"          MATCH
    "mom son"   -> "Mom and Son Playing.mp4"          MATCH
    "MomSon"    -> "Mom and Son Playing.mp4"          MATCH
    "woods bear" -> "Deer and Bear in the Woods.mp4" MATCH

The file extension is ignored for search purposes.
"""

# ============================================================================
# CONSTANTS
# ============================================================================

_COMBINING_MARK_PATTERN = re.compile(r"[\u0300-\u036f]+")
_PUNCTUATION_PATTERN = re.compile(r"[^\w\s]", re.UNICODE)
_EXTENSION_PATTERN = re.compile(r"\.[A-Za-z0-9]{1,10}$")


# ============================================================================
# TEXT NORMALIZATION
# ============================================================================


def normalize_search_text(value: str | None) -> str:
    """
    Normalize user-entered search text.

    The normalization process:

        1. Converts the value to a string.
        2. Trims leading and trailing whitespace.
        3. Applies Unicode NFKD normalization.
        4. Removes combining marks such as accents.
        5. Applies case folding for case-insensitive comparison.
        6. Converts punctuation to spaces.
        7. Collapses repeated whitespace.
        8. Trims the final result.

    Args:
        value: User-entered search text.

    Returns:
        A normalized search string.
    """
    if value is None:
        return ""

    text = str(value).strip()

    if not text:
        return ""

    text = unicodedata.normalize("NFKD", text)
    text = _COMBINING_MARK_PATTERN.sub("", text)
    text = text.casefold()
    text = _PUNCTUATION_PATTERN.sub(" ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def normalize_filename(filename: str | None) -> str:
    """
    Normalize a video filename for searching.

    The file extension is removed before normalization.

    Args:
        filename: Video filename.

    Returns:
        A normalized filename without its file extension.
    """
    if filename is None:
        return ""

    text = str(filename).strip()

    if not text:
        return ""

    text = _EXTENSION_PATTERN.sub("", text)

    return normalize_search_text(text)


def normalize_filename_compact(filename: str | None) -> str:
    """
    Return a compact normalized filename.

    All whitespace is removed.

    Example:

        "Mom and Son.mp4" -> "momandson"
    """
    normalized = normalize_filename(filename)

    if not normalized:
        return ""

    return re.sub(r"\s+", "", normalized)


# ============================================================================
# SEARCH TERM HELPERS
# ============================================================================


def split_search_terms(search_text: str | None) -> list[str]:
    """
    Split user search text into individual search terms.

    Examples:

        "deer bear" -> ["deer", "bear"]
        "deer-bear" -> ["deer", "bear"]
        "Mom, Son"  -> ["mom", "son"]
        ""          -> []
    """
    normalized = normalize_search_text(search_text)

    if not normalized:
        return []

    return [term for term in normalized.split() if term]


def _term_matches_filename(
    search_term: str,
    normalized_filename: str,
    compact_filename: str,
) -> bool:
    """
    Determine whether one search term matches a normalized filename.

    A normal substring match is attempted first.

    If that fails, a compact comparison is attempted. This allows:

        search:
            MomSon

        filename:
            Mom and Son

    to match because the compact filename contains:

        momson

    Args:
        search_term: One normalized search term.
        normalized_filename: Normalized filename.
        compact_filename: Normalized filename with whitespace removed.

    Returns:
        True if the search term matches the filename; otherwise False.
    """
    if not search_term:
        return True

    if search_term in normalized_filename:
        return True

    compact_term = re.sub(r"\s+", "", search_term)

    if not compact_term:
        return False

    return compact_term in compact_filename


# ============================================================================
# FILENAME MATCHING
# ============================================================================


def filename_matches_search(
    filename: str | None,
    search_text: str | None,
) -> bool:
    """
    Determine whether a filename matches the supplied search text.

    All search terms must match.

    Search terms are:

        - case insensitive
        - order independent
        - partial matches
        - tolerant of punctuation and separators

    Examples:

        "Deer and Bear in the Woods.mp4" + "deer"
            -> True

        "Deer and Bear in the Woods.mp4" + "deer bear"
            -> True

        "Deer and Bear in the Woods.mp4" + "bear deer"
            -> True

        "Deer and Bear in the Woods.mp4" + "deer dog"
            -> False

        "Mom and Son Playing.mp4" + "MomSon"
            -> True

    Args:
        filename: Video filename to test.
        search_text: User-entered search text.

    Returns:
        True if the filename matches all search terms; otherwise False.
    """
    normalized_filename = normalize_filename(filename)

    if not normalized_filename:
        return False

    search_terms = split_search_terms(search_text)

    if not search_terms:
        return True

    compact_filename = normalize_filename_compact(filename)

    for search_term in search_terms:
        if not _term_matches_filename(
            search_term,
            normalized_filename,
            compact_filename,
        ):
            return False

    return True


# ============================================================================
# SEARCH SCORE
# ============================================================================


def calculate_filename_match_score(
    filename: str | None,
    search_text: str | None,
) -> int:
    """
    Calculate a simple relevance score for a filename.

    Scoring rules:

        +100  Exact normalized filename match.
        +50   Normalized filename starts with the search text.
        +25   Normalized filename contains the complete search text.
        +10   Each individual search term matches.

    A score of 0 means there is no match.

    Args:
        filename: Video filename to score.
        search_text: User-entered search text.

    Returns:
        An integer relevance score.
    """
    normalized_filename = normalize_filename(filename)

    if not normalized_filename:
        return 0

    normalized_search = normalize_search_text(search_text)

    if not normalized_search:
        return 0

    if not filename_matches_search(
        filename,
        search_text,
    ):
        return 0

    score = 0

    if normalized_filename == normalized_search:
        score += 100

    if normalized_filename.startswith(normalized_search):
        score += 50

    if normalized_search in normalized_filename:
        score += 25

    search_terms = split_search_terms(search_text)

    score += len(search_terms) * 10

    return score


# ============================================================================
# SORTING
# ============================================================================


def sort_video_filenames_by_match(
    filenames: list[str],
    search_text: str | None,
) -> list[str]:
    """
    Return matching filenames ordered from strongest to weakest match.

    The original list is not modified.

    Only filenames that actually match the search are returned.

    Args:
        filenames: List of video filenames.
        search_text: User-entered search text.

    Returns:
        A new list containing only matching filenames, sorted by
        relevance score and then normalized filename.
    """
    matching_filenames = [
        filename
        for filename in filenames
        if filename_matches_search(
            filename,
            search_text,
        )
    ]

    return sorted(
        matching_filenames,
        key=lambda filename: (
            -calculate_filename_match_score(
                filename,
                search_text,
            ),
            normalize_filename(filename),
        ),
    )
# ============================================================================
# VIDEO_MODEL_SEARCH_UTILS.PY
# ============================================================================
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

    "deer"       -> "Deer and Bear in the Woods.mp4"       MATCH
    "deer bear"  -> "Deer and Bear in the Woods.mp4"       MATCH
    "bear deer"  -> "Deer and Bear in the Woods.mp4"       MATCH
    "mom"        -> "Mom and Son Playing.mp4"              MATCH
    "mom son"    -> "Mom and Son Playing.mp4"              MATCH
    "MomSon"     -> "Mom and Son Playing.mp4"              MATCH
    "woods bear" -> "Deer and Bear in the Woods.mp4"       MATCH

The file extension is ignored for search purposes.
"""

import re
import unicodedata

# ============================================================================
# CONSTANTS
# ============================================================================

# Characters that commonly separate words in filenames.
_SEPARATOR_PATTERN = re.compile(r"[\s._\-]+")

# Characters that should be treated as removable punctuation.
_PUNCTUATION_PATTERN = re.compile(r"[^\w\s]", re.UNICODE)

# Unicode combining marks are removed after normalization so that:
#
#     café -> cafe
#
# This makes searches more forgiving without changing the original filename.
_COMBINING_MARK_PATTERN = re.compile(r"[\u0300-\u036f]+")

# File extensions are removed from the filename before searching.
_EXTENSION_PATTERN = re.compile(r"\.[A-Za-z0-9]{1,10}$")


# ============================================================================
# TEXT NORMALIZATION
# ============================================================================


def normalize_search_text(value: str | None) -> str:
    """
    Normalize user-entered search text.

    The returned value is:

        - converted to a string
        - Unicode normalized
        - converted to lowercase
        - stripped of accents/combining marks
        - punctuation converted to spaces
        - repeated whitespace collapsed
        - leading/trailing whitespace removed

    Examples:

        "Mom Son"       -> "mom son"
        "MOM-SON"       -> "mom son"
        "  Deer   Bear " -> "deer bear"
        "Café"          -> "cafe"
    """
    if value is None:
        return ""

    text = str(value).strip()

    if not text:
        return ""

    text = unicodedata.normalize(
        "NFKD",
        text,
    )

    text = _COMBINING_MARK_PATTERN.sub(
        "",
        text,
    )

    text = text.casefold()

    text = _PUNCTUATION_PATTERN.sub(
        " ",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def normalize_filename(filename: str | None) -> str:
    """
    Normalize a video filename for searching.

    The file extension is removed before normalization.

    Examples:

        "Mom and Son.mp4"   -> "mom and son"
        "Mom-Son.mp4"       -> "mom son"
        "DEER_BEAR.MKV"     -> "deer bear"
        "movie"             -> "movie"
    """
    if filename is None:
        return ""

    text = str(filename).strip()

    if not text:
        return ""

    text = _EXTENSION_PATTERN.sub(
        "",
        text,
    )

    return normalize_search_text(text)


def normalize_filename_compact(filename: str | None) -> str:
    """
    Return a compact normalized filename.

    All whitespace and separators are removed.

    This representation is useful for searches such as:

        Search:
            MomSon

        Filename:
            Mom and Son.mp4

    Both can be compared using their compact representations.

    Example:

        "Mom and Son.mp4" -> "momandson"
        "Mom-Son.mp4"     -> "momson"
        "Mom_Son.mp4"     -> "momson"
    """
    normalized = normalize_filename(filename)

    if not normalized:
        return ""

    return re.sub(
        r"[\s]+",
        "",
        normalized,
    )


# ============================================================================
# SEARCH TERM HELPERS
# ============================================================================


def split_search_terms(search_text: str | None) -> list[str]:
    """
    Split user search text into individual search terms.

    Multiple spaces and punctuation are treated as separators.

    Examples:

        "deer bear"       -> ["deer", "bear"]
        "deer  bear"      -> ["deer", "bear"]
        "deer-bear"       -> ["deer", "bear"]
        "Mom, Son"        -> ["mom", "son"]
        ""                -> []
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

    Matching is intentionally forgiving.

    First, a normal substring match is attempted.

    If that fails, a compact comparison is attempted. This allows:

        search:
            MomSon

        filename:
            Mom and Son

    to match because the compact filename contains:

        momson

    The compact comparison is only used when the search term itself
    does not contain whitespace.
    """
    if not search_term:
        return True

    if search_term in normalized_filename:
        return True

    compact_term = re.sub(
        r"\s+",
        "",
        search_term,
    )

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

        filename:
            "Deer and Bear in the Woods.mp4"

        search:
            "deer"

        result:
            True

        search:
            "deer bear"

        result:
            True

        search:
            "bear deer"

        result:
            True

        search:
            "deer dog"

        result:
            False

    The search also supports compact searches:

        filename:
            "Mom and Son Playing.mp4"

        search:
            "MomSon"

        result:
            True
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

    A higher score indicates a stronger match.

    Scoring rules:

        +100  Exact normalized filename match.
        +50   Normalized filename starts with the search text.
        +25   Normalized filename contains the complete search text.
        +10   Each individual search term matches.

    This function does not determine whether a filename matches.
    Use filename_matches_search() for that.

    A score of 0 means there is no match.

    This gives the API layer the option to return the best matches
    first without making the Roku perform any sorting or scoring.
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

    This helper is optional and can be used later if the API should
    prioritize the most relevant results.
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

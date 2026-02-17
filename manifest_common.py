"""
Manifest Common

Shared utilities for the playlist downloader suite. Contains constants, the
ManifestEntry dataclass, and all manifest parsing/formatting logic used by
song_downloader, playlist_auditor, and playlist_fixer.

The manifest format supports an optional header line:
    #COLUMNS: index;;;video_id;;;title;;;view_count

Legacy manifests without a header are parsed as 3-field (index, video_id, title).
"""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


# Common audio/video extensions
MEDIA_EXTENSIONS: set[str] = {
    ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac", ".wma",
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv",
}

# Encodings to try when reading manifest files
ENCODINGS_TO_TRY: list[str] = [
    "utf-8",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "cp1252",
    "iso-8859-1",
    "utf-8-sig",
]

# Manifest format constants
MANIFEST_DELIMITER: str = ";;;"
MANIFEST_HEADER_PREFIX: str = "#COLUMNS:"

# Default column sets
LEGACY_COLUMNS: list[str] = ["index", "video_id", "title"]
FULL_COLUMNS: list[str] = ["index", "video_id", "title", "view_count"]


@dataclass
class ManifestEntry:
    """Represents a single entry from the playlist manifest."""

    index: int
    video_id: str
    title: str
    view_count: int | None = None  # None for legacy manifests or unavailable data

    @property
    def index_str(self) -> str:
        """Returns the 4-digit zero-padded index string."""
        return f"{self.index:04d}"


def format_manifest_header(columns: list[str]) -> str:
    """Generates a manifest header line.

    Args:
        columns: List of column names.

    Returns:
        Header string like ``#COLUMNS: index;;;video_id;;;title;;;view_count``.
    """
    return f"{MANIFEST_HEADER_PREFIX} {MANIFEST_DELIMITER.join(columns)}"


def format_manifest_line(entry: ManifestEntry, columns: list[str]) -> str:
    """Serializes a ManifestEntry according to the declared columns.

    Args:
        entry: The manifest entry to serialize.
        columns: Ordered list of column names to include.

    Returns:
        A single manifest line (no trailing newline).
    """
    field_map: dict[str, str] = {
        "index": entry.index_str,
        "video_id": entry.video_id,
        "title": entry.title,
        "view_count": str(entry.view_count) if entry.view_count is not None else "NA",
    }
    return MANIFEST_DELIMITER.join(field_map.get(col, "") for col in columns)


def validate_manifest_content(content: str) -> bool:
    """Validates that content looks like a valid manifest file.

    Accepts lines starting with digits followed by ``;;;``, as well as
    ``#COLUMNS:`` header lines.

    Args:
        content: The decoded file content.

    Returns:
        True if content appears to be a valid manifest.
    """
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(MANIFEST_HEADER_PREFIX):
            return True
        if re.match(r"^\d+;;;", line):
            return True
    return False


def read_manifest_with_encoding(manifest_path: Path) -> tuple[str, str]:
    """Attempts to read the manifest file with multiple encodings.

    Args:
        manifest_path: Path to the manifest file.

    Returns:
        Tuple of (content, encoding_used).

    Raises:
        ValueError: If no encoding could successfully read the file.
    """
    for encoding in ENCODINGS_TO_TRY:
        try:
            content = manifest_path.read_text(encoding=encoding)
            # Strip UTF-8 BOM if present
            content = content.removeprefix("\ufeff")
            if content and validate_manifest_content(content):
                return content, encoding
        except (UnicodeDecodeError, UnicodeError):
            continue

    raise ValueError(
        "Could not read manifest file with any supported encoding"
    )


def _parse_view_count(raw: str) -> int | None:
    """Parses a view count value, returning None for unavailable data."""
    raw = raw.strip()
    if not raw or raw.upper() == "NA" or raw.upper() == "NONE":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_manifest(manifest_path: Path) -> dict[int, ManifestEntry]:
    """Parses the playlist manifest file.

    Supports both header-based and legacy (3-field) formats. If a
    ``#COLUMNS:`` header is found, fields are mapped by column name.
    Otherwise falls back to the legacy ``index;;;video_id;;;title`` format.

    Args:
        manifest_path: Path to the manifest file.

    Returns:
        Dictionary mapping playlist index to ManifestEntry.
    """
    content, encoding_used = read_manifest_with_encoding(manifest_path)
    print(f"Read manifest using encoding: {encoding_used}")

    entries: dict[int, ManifestEntry] = {}
    columns: list[str] | None = None

    for line_num, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        # Check for header line
        if line.startswith(MANIFEST_HEADER_PREFIX):
            header_body = line[len(MANIFEST_HEADER_PREFIX):].strip()
            columns = [c.strip() for c in header_body.split(MANIFEST_DELIMITER)]
            continue

        # Skip other comment lines
        if line.startswith("#"):
            continue

        parts = line.split(MANIFEST_DELIMITER)

        if columns is not None:
            # Header-based parsing
            if len(parts) < len(columns):
                # Pad with empty strings for missing trailing fields
                parts.extend([""] * (len(columns) - len(parts)))

            field_map = {col: parts[i] for i, col in enumerate(columns) if i < len(parts)}

            try:
                index = int(field_map.get("index", "").strip())
                video_id = field_map.get("video_id", "").strip()
                title = field_map.get("title", "").strip()
                view_count = _parse_view_count(field_map.get("view_count", ""))
                entries[index] = ManifestEntry(
                    index=index,
                    video_id=video_id,
                    title=title,
                    view_count=view_count,
                )
            except ValueError as e:
                print(f"Warning: Could not parse line {line_num}: {e}")
                continue
        else:
            # Legacy 3-field parsing
            if len(parts) != 3:
                print(f"Warning: Skipping malformed line {line_num}: {line[:50]}...")
                continue

            try:
                index = int(parts[0])
                video_id = parts[1].strip()
                title = parts[2].strip()
                entries[index] = ManifestEntry(
                    index=index,
                    video_id=video_id,
                    title=title,
                )
            except ValueError as e:
                print(f"Warning: Could not parse line {line_num}: {e}")
                continue

    return entries


def normalize_title(title: str) -> str:
    """Normalizes a title for comparison.

    Uses NFKD unicode normalization, filters to ASCII-only, and converts
    all punctuation to spaces for robust matching across different filename
    encodings and downloader behaviors.

    Args:
        title: The title string to normalize.

    Returns:
        Normalized title for comparison purposes.
    """
    # Normalize unicode to NFKD to handle fullwidth chars and decompose accents
    normalized = unicodedata.normalize("NFKD", title.lower())

    # Filter to only ASCII characters
    normalized = "".join(c for c in normalized if ord(c) < 128)

    # Keep only alphanumeric and spaces
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)

    # Collapse multiple spaces and strip
    normalized = re.sub(r"\s+", " ", normalized).strip()

    return normalized


def extract_title_from_filename(filename: str) -> str | None:
    """Extracts the title portion from a filename.

    Handles both formats:
    - ``0001 - Title.ext`` -> ``Title``
    - `` - Title.ext`` -> ``Title``

    Args:
        filename: The filename (without directory path).

    Returns:
        The extracted title, or None if extraction failed.
    """
    stem = Path(filename).stem

    indexed_match = re.match(r"^\d{4}\s*-\s*(.+)$", stem)
    if indexed_match:
        return indexed_match.group(1).strip()

    non_indexed_match = re.match(r"^\s*-\s*(.+)$", stem)
    if non_indexed_match:
        return non_indexed_match.group(1).strip()

    return None


def get_file_index(filename: str) -> int | None:
    """Extracts the playlist index from a filename if present.

    Args:
        filename: The filename to check.

    Returns:
        The index as an integer, or None if not present.
    """
    stem = Path(filename).stem
    match = re.match(r"^(\d{4})\s*-\s*", stem)
    if match:
        return int(match.group(1))
    return None


def is_media_file(path: Path) -> bool:
    """Checks if a file is a media file based on extension."""
    return path.suffix.lower() in MEDIA_EXTENSIONS


def find_matching_entry(
    filename: str,
    manifest: dict[int, ManifestEntry],
) -> ManifestEntry | None:
    """Finds the manifest entry that matches a given filename by title.

    Args:
        filename: The filename to match.
        manifest: The parsed manifest dictionary.

    Returns:
        The matching ManifestEntry, or None if no match found.
    """
    file_title = extract_title_from_filename(filename)
    if not file_title:
        return None

    normalized_file_title = normalize_title(file_title)

    for entry in manifest.values():
        normalized_entry_title = normalize_title(entry.title)
        if normalized_file_title == normalized_entry_title:
            return entry

    return None

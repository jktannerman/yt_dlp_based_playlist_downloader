"""
Playlist Auditor

Compares song files in a folder against a manifest file and reports
discrepancies (missing and unexpected songs). This is a read-only version
of the playlist_fixer.py script.

Expected manifest format per line:
0001;;;VIDEO_ID;;;Title

Expected filename format: %(playlist_index)04d - %(title)s.%(ext)s
Example: 0001 - Flight [Monstercat Release].mp3
"""

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# Common audio/video extensions
MEDIA_EXTENSIONS: set[str] = {
    # Audio
    ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac", ".wma",
    # Video
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv",
}

# Encodings to try when reading the manifest file
ENCODINGS_TO_TRY: list[str] = [
    "utf-8",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "cp1252",     # Windows Western European
    "iso-8859-1", # Latin-1
    "utf-8-sig",  # UTF-8 with BOM (try last)
]


@dataclass
class ManifestEntry:
    """Represents a single entry from the playlist manifest."""
    index: int
    video_id: str
    title: str

    @property
    def index_str(self) -> str:
        """Returns the 4-digit zero-padded index string."""
        return f"{self.index:04d}"


@dataclass
class AuditReport:
    """Collects all discrepancies found during the audit."""
    missing_songs: list[ManifestEntry] = field(default_factory=list)
    unexpected_songs: list[str] = field(default_factory=list)
    matched_songs: list[tuple[str, ManifestEntry]] = field(default_factory=list)

    def has_issues(self) -> bool:
        """Returns True if there are any discrepancies."""
        return bool(self.missing_songs or self.unexpected_songs)


def validate_manifest_content(content: str) -> bool:
    """
    Validates that content looks like a valid manifest file.

    Checks that at least one non-empty line matches the expected format:
    4-digit number followed by ";;;".

    Args:
        content: The decoded file content.

    Returns:
        True if content appears to be a valid manifest.
    """
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        # Check if line starts with 4 digits followed by ";;;"
        if re.match(r"^\d{4};;;", line):
            return True
    return False


def read_manifest_with_encoding(manifest_path: Path) -> tuple[str, str]:
    """
    Attempts to read the manifest file with multiple encodings.

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
            # Validate that content matches expected manifest format
            if content and validate_manifest_content(content):
                return content, encoding
        except (UnicodeDecodeError, UnicodeError):
            continue

    raise ValueError(
        f"Could not read manifest file with any of these encodings: "
        f"{', '.join(ENCODINGS_TO_TRY)}"
    )


def parse_manifest(manifest_path: Path) -> dict[int, ManifestEntry]:
    """
    Parses the playlist manifest file.

    Expected format per line:
    0001;;;OVMuwa-HRCQ;;;[Drumstep] - Tristam & Braken - Flight [Monstercat Release]

    Args:
        manifest_path: Path to the manifest file.

    Returns:
        Dictionary mapping playlist index to ManifestEntry.
    """
    content, encoding_used = read_manifest_with_encoding(manifest_path)
    print(f"Read manifest using encoding: {encoding_used}")

    entries: dict[int, ManifestEntry] = {}

    for line_num, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        # Split by ';;;' delimiter
        parts = line.split(";;;")
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
    """
    Normalizes a title for comparison by removing/replacing problematic characters.

    This handles characters that are invalid in Windows filenames and may have
    been replaced or removed during download. Also filters out non-ASCII characters
    to avoid false positives from encoding differences between filenames and manifest.

    Some programs convert special characters (colons, quotes, etc.) to dashes,
    so we normalize all such punctuation to spaces for consistent comparison.

    Args:
        title: The title string to normalize.

    Returns:
        Normalized title for comparison purposes.
    """
    # First, normalize unicode to NFKD to handle fullwidth chars and decompose accents
    normalized = unicodedata.normalize('NFKD', title.lower())

    # Filter to only ASCII characters (removes accents, special unicode chars, etc.)
    normalized = ''.join(c for c in normalized if ord(c) < 128)

    # Convert all punctuation that may be transformed by downloaders to spaces
    # This includes: - : " ' / \ | ? * < > [ ] ( ) _ and similar
    # Keep only alphanumeric and spaces
    normalized = re.sub(r'[^a-z0-9\s]', ' ', normalized)

    # Collapse multiple spaces and strip
    normalized = re.sub(r'\s+', ' ', normalized).strip()

    return normalized


def extract_title_from_filename(filename: str) -> str | None:
    """
    Extracts the title portion from a filename.

    Handles both formats:
    - "0001 - Title.ext" -> "Title"
    - " - Title.ext" -> "Title"

    Args:
        filename: The filename (without directory path).

    Returns:
        The extracted title, or None if extraction failed.
    """
    # Remove extension
    stem = Path(filename).stem

    # Check for indexed format: "0001 - Title"
    indexed_match = re.match(r"^\d{4}\s*-\s*(.+)$", stem)
    if indexed_match:
        return indexed_match.group(1).strip()

    # Check for non-indexed format: " - Title"
    non_indexed_match = re.match(r"^\s*-\s*(.+)$", stem)
    if non_indexed_match:
        return non_indexed_match.group(1).strip()

    return None


def get_file_index(filename: str) -> int | None:
    """
    Extracts the playlist index from a filename if present.

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
    """
    Finds the manifest entry that matches a given filename by title.

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


def audit_playlist_files(
    songs_folder: Path,
    manifest_path: Path,
) -> AuditReport:
    """
    Audits playlist files against a manifest (read-only).

    Args:
        songs_folder: Path to the folder containing song files.
        manifest_path: Path to the playlist manifest file.

    Returns:
        AuditReport containing all discrepancies found.
    """
    report = AuditReport()

    # Parse manifest
    manifest = parse_manifest(manifest_path)
    print(f"Loaded {len(manifest)} entries from manifest")

    # Track which manifest entries we've seen
    seen_indices: set[int] = set()

    # Scan all media files in the folder
    media_files = [f for f in songs_folder.iterdir() if f.is_file() and is_media_file(f)]
    print(f"Found {len(media_files)} media files")

    for file_path in media_files:
        filename = file_path.name
        file_index = get_file_index(filename)

        if file_index is not None:
            # File has an index - verify both index AND title match
            if file_index in manifest:
                entry = manifest[file_index]
                file_title = extract_title_from_filename(filename)

                if file_title is not None:
                    # Compare normalized titles
                    normalized_file_title = normalize_title(file_title)
                    normalized_entry_title = normalize_title(entry.title)

                    if normalized_file_title == normalized_entry_title:
                        # Both index and title match
                        seen_indices.add(file_index)
                        report.matched_songs.append((filename, entry))
                    else:
                        # Index matches but title doesn't - unexpected
                        report.unexpected_songs.append(filename)
                else:
                    # Couldn't extract title - unexpected
                    report.unexpected_songs.append(filename)
            else:
                # Has index but not in manifest - unexpected
                report.unexpected_songs.append(filename)
        else:
            # File has no index - try to match by title
            entry = find_matching_entry(filename, manifest)

            if entry:
                # Found a match by title
                seen_indices.add(entry.index)
                report.matched_songs.append((filename, entry))
            else:
                # No match found - unexpected file
                report.unexpected_songs.append(filename)

    # Find missing songs (in manifest but not found)
    for index, entry in manifest.items():
        if index not in seen_indices:
            report.missing_songs.append(entry)

    return report


def print_report(report: AuditReport) -> None:
    """
    Prints the audit report to stdout.

    Args:
        report: The AuditReport to print.
    """
    print()
    print("=" * 60)
    print("AUDIT SUMMARY")
    print("=" * 60)
    print(f"Matched songs:     {len(report.matched_songs)}")
    print(f"Missing songs:     {len(report.missing_songs)}")
    print(f"Unexpected songs:  {len(report.unexpected_songs)}")
    print()

    # Missing songs
    if report.missing_songs:
        print("MISSING SONGS (in manifest but not found)")
        print("-" * 40)
        for entry in sorted(report.missing_songs, key=lambda e: e.index):
            print(f"  [{entry.index_str}] {entry.title}")
        print()

    # Unexpected songs
    if report.unexpected_songs:
        print("UNEXPECTED SONGS (found but not in manifest)")
        print("-" * 40)
        for filename in sorted(report.unexpected_songs):
            print(f"  {filename}")
        print()

    if not report.has_issues():
        print("No discrepancies found.")
        print()


def write_report(report: AuditReport, output_path: Path) -> None:
    """
    Writes the audit report to a text file.

    Args:
        report: The AuditReport to write.
        output_path: Path for the output file.
    """
    lines: list[str] = []

    lines.append("=" * 60)
    lines.append("PLAYLIST AUDIT REPORT")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)
    lines.append("")

    # Summary
    lines.append("SUMMARY")
    lines.append("-" * 40)
    lines.append(f"Matched songs:     {len(report.matched_songs)}")
    lines.append(f"Missing songs:     {len(report.missing_songs)}")
    lines.append(f"Unexpected songs:  {len(report.unexpected_songs)}")
    lines.append("")

    # Missing songs
    if report.missing_songs:
        lines.append("MISSING SONGS (in manifest but not found)")
        lines.append("-" * 40)
        for entry in sorted(report.missing_songs, key=lambda e: e.index):
            lines.append(f"  [{entry.index_str}] {entry.title}")
            lines.append(f"           Video ID: {entry.video_id}")
        lines.append("")

    # Unexpected songs
    if report.unexpected_songs:
        lines.append("UNEXPECTED SONGS (found but not in manifest)")
        lines.append("-" * 40)
        for filename in sorted(report.unexpected_songs):
            lines.append(f"  {filename}")
        lines.append("")

    # Matched songs (for completeness)
    if report.matched_songs:
        lines.append("MATCHED SONGS")
        lines.append("-" * 40)
        for filename, entry in sorted(report.matched_songs, key=lambda x: x[1].index):
            lines.append(f"  [{entry.index_str}] {filename}")
        lines.append("")

    lines.append("=" * 60)
    lines.append("END OF REPORT")
    lines.append("=" * 60)

    # Write with utf-8-sig for best Windows compatibility
    output_path.write_text("\n".join(lines), encoding="utf-8-sig")
    print(f"Report written to: {output_path}")


def main() -> int:
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Audit playlist files against a manifest (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s C:\\Music\\Playlist C:\\Music\\playlist_manifest.txt
  %(prog)s ./songs ./manifest.txt
  %(prog)s ./songs ./manifest.txt -o ./audit_report.txt
        """,
    )

    parser.add_argument(
        "songs_folder",
        type=Path,
        help="Path to the folder containing the song files",
    )

    parser.add_argument(
        "manifest_file",
        type=Path,
        help="Path to the playlist manifest file",
    )

    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Path for the output report file (optional)",
    )

    args = parser.parse_args()

    # Validate paths
    if not args.songs_folder.is_dir():
        print(f"Error: Songs folder not found: {args.songs_folder}")
        return 1

    if not args.manifest_file.is_file():
        print(f"Error: Manifest file not found: {args.manifest_file}")
        return 1

    # Run the audit
    print(f"Songs folder: {args.songs_folder}")
    print(f"Manifest file: {args.manifest_file}")
    print("-" * 40)

    report = audit_playlist_files(
        songs_folder=args.songs_folder,
        manifest_path=args.manifest_file,
    )

    # Print report to console
    print_report(report)

    # Write report to file if requested
    if args.output:
        write_report(report, args.output)

    return 0 if not report.has_issues() else 1


if __name__ == "__main__":
    sys.exit(main())

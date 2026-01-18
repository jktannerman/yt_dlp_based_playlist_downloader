"""
Playlist Filename Fixer

Scans a folder of downloaded songs and performs two operations:
1. Delete duplicates: Remove songs whose titles have appeared before at a lower index
2. Fix offset errors: Rename files with wrong indices to correct indices based on title matching

Uses a manifest file to determine the correct indices for each title.

Expected filename format: %(playlist_index)04d - %(title)s.%(ext)s
Example: 0001 - Flight [Monstercat Release].mp3
"""

import argparse
import re
import shutil
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
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
class FixerReport:
    """Collects all changes and discrepancies for the final report."""
    deleted_duplicates: list[tuple[str, str]] = field(default_factory=list)  # (filename, reason)
    renamed_files: list[tuple[str, str]] = field(default_factory=list)  # (old_name, new_name)
    already_correct: list[str] = field(default_factory=list)
    unmatched_files: list[str] = field(default_factory=list)  # Files that don't match any manifest entry
    errors: list[str] = field(default_factory=list)

    def has_issues(self) -> bool:
        """Returns True if there are any issues to report."""
        return bool(
            self.deleted_duplicates or
            self.renamed_files or
            self.unmatched_files or
            self.errors
        )


def validate_manifest_content(content: str) -> bool:
    """
    Validates that content looks like a valid manifest file.

    Checks that at least one non-empty line matches the expected format:
    4-digit number followed by " | ".

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


def safe_rename_file(src: Path, dst: Path, timeout: float = 5.0) -> None:
    """
    Safely rename a file using copy-then-delete strategy with timeout.

    This approach is more robust on Windows where Path.rename() can hang
    indefinitely when files are locked by indexers, antivirus, etc.

    Args:
        src: Source file path.
        dst: Destination file path.
        timeout: Maximum seconds to wait for the operation (default: 30).

    Raises:
        OSError: If copy or delete fails.
        FileExistsError: If destination already exists.
        TimeoutError: If the operation exceeds the timeout.
    """
    if dst.exists():
        raise FileExistsError(f"Destination already exists: {dst}")

    def _do_copy() -> None:
        shutil.copy2(src, dst)

    def _do_delete() -> None:
        src.unlink()

    with ThreadPoolExecutor(max_workers=1) as executor:
        # Copy with metadata preservation
        future = executor.submit(_do_copy)
        try:
            future.result(timeout=timeout)
        except FuturesTimeoutError:
            raise TimeoutError(f"Copy timed out after {timeout}s: {src}")

        # Verify copy succeeded
        if not dst.exists():
            raise OSError(f"Copy failed - destination not created: {dst}")

        # Delete original
        future = executor.submit(_do_delete)
        try:
            future.result(timeout=timeout)
        except FuturesTimeoutError:
            raise TimeoutError(f"Delete timed out after {timeout}s: {src}")


def safe_delete_file(file_path: Path, timeout: float = 5.0) -> None:
    """
    Safely delete a file with timeout protection.

    Args:
        file_path: Path to the file to delete.
        timeout: Maximum seconds to wait for the operation (default: 5).

    Raises:
        OSError: If delete fails.
        FileNotFoundError: If file doesn't exist.
        TimeoutError: If the operation exceeds the timeout.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    def _do_delete() -> None:
        file_path.unlink()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_do_delete)
        try:
            future.result(timeout=timeout)
        except FuturesTimeoutError:
            raise TimeoutError(f"Delete timed out after {timeout}s: {file_path}")


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


def build_manifest_by_title(
    manifest: dict[int, ManifestEntry],
) -> dict[str, ManifestEntry]:
    """
    Builds a lookup dictionary from normalized title to manifest entry.

    Args:
        manifest: The parsed manifest dictionary.

    Returns:
        Dictionary mapping normalized title to ManifestEntry.
    """
    result: dict[str, ManifestEntry] = {}
    for entry in manifest.values():
        norm_title = normalize_title(entry.title)
        result[norm_title] = entry
    return result


def fix_playlist_files(
    songs_folder: Path,
    manifest_path: Path,
    dry_run: bool = False,
) -> FixerReport:
    """
    Main function to fix playlist file names.

    Performs two operations:
    1. Delete duplicates: Remove songs whose titles have appeared before at a lower index
    2. Fix offset errors: Rename files with wrong indices to correct indices based on title matching

    Args:
        songs_folder: Path to the folder containing song files.
        manifest_path: Path to the playlist manifest file.
        dry_run: If True, don't actually make changes.

    Returns:
        FixerReport containing all changes and discrepancies.
    """
    report = FixerReport()

    # ========== PHASE 1: Build data structures ==========
    print("Phase 1: Building data structures...")

    # Parse manifest
    try:
        manifest = parse_manifest(manifest_path)
        print(f"  Loaded {len(manifest)} entries from manifest")
    except Exception as e:
        report.errors.append(f"Failed to parse manifest: {e}")
        return report

    manifest_by_title = build_manifest_by_title(manifest)

    # Scan all media files
    media_files = [f for f in songs_folder.iterdir() if f.is_file() and is_media_file(f)]
    print(f"  Found {len(media_files)} media files")

    # Build files_by_index: dict[int, Path]
    files_by_index: dict[int, Path] = {}
    # Build files_by_normalized_title: dict[str, list[tuple[int, Path]]]
    files_by_normalized_title: dict[str, list[tuple[int, Path]]] = {}

    for file_path in media_files:
        filename = file_path.name
        file_index = get_file_index(filename)
        file_title = extract_title_from_filename(filename)

        if file_index is not None:
            files_by_index[file_index] = file_path

        if file_title:
            norm_title = normalize_title(file_title)
            if norm_title not in files_by_normalized_title:
                files_by_normalized_title[norm_title] = []
            # Use file_index or a large number if no index (for sorting purposes)
            idx = file_index if file_index is not None else 999999
            files_by_normalized_title[norm_title].append((idx, file_path))

    # ========== PHASE 2: Delete duplicates ==========
    print("Phase 2: Deleting duplicates...")

    # Track files to delete and files deleted
    files_deleted: set[Path] = set()

    for norm_title, file_list in files_by_normalized_title.items():
        if len(file_list) <= 1:
            continue

        # Skip very short normalized titles - likely lost most characters to encoding
        if len(norm_title) < 3:
            continue

        # Sort by index (ascending) - keep the first (lowest index)
        file_list_sorted = sorted(file_list, key=lambda x: x[0])
        kept_file = file_list_sorted[0]

        for idx, file_path in file_list_sorted[1:]:
            reason = f"Duplicate of index {kept_file[0]:04d} (same title: '{norm_title[:50]}...')"
            if not dry_run:
                try:
                    print(f"  Deleting duplicate: {file_path.name}")
                    safe_delete_file(file_path)
                    files_deleted.add(file_path)
                    report.deleted_duplicates.append((file_path.name, reason))
                    # Update files_by_index if this file had an index
                    if idx in files_by_index and files_by_index[idx] == file_path:
                        del files_by_index[idx]
                except (OSError, FileNotFoundError, TimeoutError) as e:
                    report.errors.append(f"Failed to delete '{file_path.name}': {e}")
            else:
                report.deleted_duplicates.append((file_path.name, reason))
                files_deleted.add(file_path)
                if idx in files_by_index and files_by_index[idx] == file_path:
                    del files_by_index[idx]

    print(f"  Duplicates to delete: {len(report.deleted_duplicates)}")

    # ========== PHASE 3: Fix offset errors ==========
    print("Phase 3: Fixing offset errors...")

    # Build set of occupied indices (after deletions)
    occupied_indices: set[int] = set(files_by_index.keys())

    # Iterate over remaining files
    for file_path in media_files:
        if file_path in files_deleted:
            continue

        filename = file_path.name
        file_index = get_file_index(filename)
        file_title = extract_title_from_filename(filename)

        if not file_title:
            report.unmatched_files.append(filename)
            continue

        norm_title = normalize_title(file_title)

        # Look up manifest entry by title
        manifest_entry = manifest_by_title.get(norm_title)

        if not manifest_entry:
            report.unmatched_files.append(filename)
            continue

        expected_index = manifest_entry.index

        if file_index == expected_index:
            # Already correct
            report.already_correct.append(filename)
            continue

        # File index is wrong - check if we can rename
        if expected_index in occupied_indices:
            # Conflict - correct slot is occupied
            report.errors.append(
                f"Cannot rename '{filename}' to index {expected_index:04d}: slot occupied"
            )
            continue

        # Build new filename
        extension = file_path.suffix
        # Sanitize title for Windows filename
        invalid_chars = r'<>:"/\|?*'
        sanitized_title = manifest_entry.title
        for char in invalid_chars:
            sanitized_title = sanitized_title.replace(char, '-')
        new_filename = f"{expected_index:04d} - {sanitized_title}{extension}"
        new_path = file_path.parent / new_filename

        if not dry_run:
            try:
                print(f"  Renaming: {filename}")
                print(f"       -> {new_filename}")
                safe_rename_file(file_path, new_path)
                report.renamed_files.append((filename, new_filename))
                # Update occupied indices
                if file_index is not None and file_index in occupied_indices:
                    occupied_indices.discard(file_index)
                occupied_indices.add(expected_index)
            except (OSError, FileExistsError, TimeoutError) as e:
                report.errors.append(f"Failed to rename '{filename}': {e}")
        else:
            report.renamed_files.append((filename, new_filename))
            if file_index is not None:
                occupied_indices.discard(file_index)
            occupied_indices.add(expected_index)

    print(f"  Files to rename: {len(report.renamed_files)}")
    print(f"  Already correct: {len(report.already_correct)}")
    print(f"  Unmatched files: {len(report.unmatched_files)}")

    return report


def write_report(report: FixerReport, output_path: Path, dry_run: bool) -> None:
    """
    Writes the fix report to a text file.

    Args:
        report: The FixerReport to write.
        output_path: Path for the output file.
        dry_run: Whether this was a dry run.
    """
    lines: list[str] = []

    lines.append("=" * 60)
    lines.append("PLAYLIST FILENAME FIXER REPORT")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if dry_run:
        lines.append("MODE: DRY RUN (no actual changes made)")
    lines.append("=" * 60)
    lines.append("")

    # Summary
    lines.append("SUMMARY")
    lines.append("-" * 40)
    lines.append(f"Duplicates deleted: {len(report.deleted_duplicates)}")
    lines.append(f"Files renamed:      {len(report.renamed_files)}")
    lines.append(f"Already correct:    {len(report.already_correct)}")
    lines.append(f"Unmatched files:    {len(report.unmatched_files)}")
    lines.append(f"Errors:             {len(report.errors)}")
    lines.append("")

    # Deleted duplicates
    if report.deleted_duplicates:
        lines.append("DELETED DUPLICATES")
        lines.append("-" * 40)
        for filename, reason in report.deleted_duplicates:
            lines.append(f"  FILE: {filename}")
            lines.append(f"  REASON: {reason}")
            lines.append("")

    # Renamed files
    if report.renamed_files:
        lines.append("RENAMED FILES")
        lines.append("-" * 40)
        for old_name, new_name in report.renamed_files:
            lines.append(f"  OLD: {old_name}")
            lines.append(f"  NEW: {new_name}")
            lines.append("")

    # Unmatched files
    if report.unmatched_files:
        lines.append("UNMATCHED FILES (no manifest entry found)")
        lines.append("-" * 40)
        for filename in sorted(report.unmatched_files):
            lines.append(f"  {filename}")
        lines.append("")

    # Errors
    if report.errors:
        lines.append("ERRORS")
        lines.append("-" * 40)
        for error in report.errors:
            lines.append(f"  {error}")
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
        description="Fix playlist filenames that are missing indices.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s C:\\Music\\Playlist C:\\Music\\playlist_manifest.txt
  %(prog)s ./songs ./manifest.txt --dry-run
  %(prog)s ./songs ./manifest.txt -o ./fix_report.txt
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
        help="Path for the output report file (default: <songs_folder>/fix_report.txt)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )

    args = parser.parse_args()

    # Validate paths
    if not args.songs_folder.is_dir():
        print(f"Error: Songs folder not found: {args.songs_folder}")
        return 1

    if not args.manifest_file.is_file():
        print(f"Error: Manifest file not found: {args.manifest_file}")
        return 1

    # Set default output path
    output_path = args.output or (args.songs_folder / "fix_report.txt")

    # Run the fixer
    print(f"Songs folder: {args.songs_folder}")
    print(f"Manifest file: {args.manifest_file}")
    print(f"Dry run: {args.dry_run}")
    print("-" * 40)

    report = fix_playlist_files(
        songs_folder=args.songs_folder,
        manifest_path=args.manifest_file,
        dry_run=args.dry_run,
    )

    # Write report
    write_report(report, output_path, args.dry_run)

    # Print summary to console
    print("-" * 40)
    print(f"Deleted duplicates: {len(report.deleted_duplicates)}")
    print(f"Renamed: {len(report.renamed_files)} files")
    print(f"Unmatched: {len(report.unmatched_files)} files")

    if report.errors:
        print(f"Errors: {len(report.errors)}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

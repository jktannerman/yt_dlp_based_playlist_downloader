"""
Song Downloader

Downloads missing songs from YouTube based on a playlist.
First fetches the playlist manifest using yt-dlp, then downloads missing songs.

Usage:
    py -3.13 song_downloader.py <songs_folder> <playlist_id>
    py -3.13 song_downloader.py <songs_folder> --use-manifest <manifest_file>

Expected manifest format per line:
0001;;;VIDEO_ID;;;Song Title
"""

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# Common audio/video extensions
MEDIA_EXTENSIONS: set[str] = {
    ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac", ".wma",
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv",
}

# Encodings to try when reading the manifest file
ENCODINGS_TO_TRY: list[str] = [
    "utf-8",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "cp1252",
    "iso-8859-1",
    "utf-8-sig",
]

# Download settings
DOWNLOAD_DELAY_SECONDS: int = 15


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
class DownloadReport:
    """Collects all download outcomes for the final report."""
    downloaded: list[ManifestEntry] = field(default_factory=list)
    already_exists: list[ManifestEntry] = field(default_factory=list)
    failed: list[tuple[ManifestEntry, str]] = field(default_factory=list)
    skipped_dry_run: list[ManifestEntry] = field(default_factory=list)


def validate_manifest_content(content: str) -> bool:
    """Validates that content looks like a valid manifest file."""
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^\d{4};;;", line):
            return True
    return False


def read_manifest_with_encoding(manifest_path: Path) -> tuple[str, str]:
    """Attempts to read the manifest file with multiple encodings."""
    for encoding in ENCODINGS_TO_TRY:
        try:
            content = manifest_path.read_text(encoding=encoding)
            if content and validate_manifest_content(content):
                return content, encoding
        except (UnicodeDecodeError, UnicodeError):
            continue

    raise ValueError(
        f"Could not read manifest file with any supported encoding"
    )


def parse_manifest(manifest_path: Path) -> dict[int, ManifestEntry]:
    """Parses the playlist manifest file."""
    content, encoding_used = read_manifest_with_encoding(manifest_path)
    print(f"Read manifest using encoding: {encoding_used}")

    entries: dict[int, ManifestEntry] = {}

    for line_num, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        parts = line.split(";;;")
        if len(parts) != 3:
            print(f"Warning: Skipping malformed line {line_num}: {line[:50]}...")
            continue

        try:
            index = int(parts[0])
            video_id = parts[1].strip()
            title = parts[2].strip()
            entries[index] = ManifestEntry(index=index, video_id=video_id, title=title)
        except ValueError as e:
            print(f"Warning: Could not parse line {line_num}: {e}")
            continue

    return entries


def normalize_title(title: str) -> str:
    """Normalizes a title for comparison."""
    normalized = title.lower()
    replacements = [
        ('"', ''), ('\uff02', ''), ("'", ''),
        (':', ' -'), ('\uff1a', ' -'),
        ('/', '-'), ('\uff0f', '-'), ('\u29f8', '-'),
        ('\\', '-'), ('\uff3c', '-'),
        ('|', '-'), ('\uff5c', '-'),
        ('?', ''), ('\uff1f', ''),
        ('*', ''), ('\uff0a', ''),
        ('<', ''), ('\uff1c', ''),
        ('>', ''), ('\uff1e', ''),
        ('  ', ' '),
    ]
    for old, new in replacements:
        normalized = normalized.replace(old, new)
    return normalized.strip()


def sanitize_filename(title: str) -> str:
    """Sanitizes a title for use in Windows filenames."""
    invalid_chars = '<>:"/\\|?*'
    sanitized = title
    for char in invalid_chars:
        sanitized = sanitized.replace(char, '-')
    return sanitized


def is_media_file(path: Path) -> bool:
    """Checks if a file is a media file based on extension."""
    return path.suffix.lower() in MEDIA_EXTENSIONS


def get_file_index(filename: str) -> int | None:
    """Extracts the playlist index from a filename if present."""
    stem = Path(filename).stem
    match = re.match(r"^(\d{4})\s*-\s*", stem)
    if match:
        return int(match.group(1))
    return None


def extract_title_from_filename(filename: str) -> str | None:
    """Extracts the title portion from a filename."""
    stem = Path(filename).stem
    indexed_match = re.match(r"^\d{4}\s*-\s*(.+)$", stem)
    if indexed_match:
        return indexed_match.group(1).strip()
    non_indexed_match = re.match(r"^\s*-\s*(.+)$", stem)
    if non_indexed_match:
        return non_indexed_match.group(1).strip()
    return None


def find_existing_indices(songs_folder: Path, manifest: dict[int, ManifestEntry]) -> set[int]:
    """Finds which manifest entries already exist in the folder."""
    existing_indices: set[int] = set()
    media_files = [f for f in songs_folder.iterdir() if f.is_file() and is_media_file(f)]

    for file_path in media_files:
        filename = file_path.name
        file_index = get_file_index(filename)

        if file_index is not None and file_index in manifest:
            existing_indices.add(file_index)
            continue

        # Try matching by normalized title
        file_title = extract_title_from_filename(filename)
        if file_title:
            normalized_file_title = normalize_title(file_title)
            for entry in manifest.values():
                if normalize_title(entry.title) == normalized_file_title:
                    existing_indices.add(entry.index)
                    break

    return existing_indices


def check_ytdlp_installed() -> bool:
    """Checks if yt-dlp is available."""
    try:
        subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


# Default manifest filename
MANIFEST_FILENAME: str = "playlist_manifest.txt"


def download_playlist_manifest(playlist_id: str, songs_folder: Path) -> tuple[bool, Path, str]:
    """
    Downloads the playlist manifest using yt-dlp.

    Args:
        playlist_id: YouTube playlist ID (e.g., PLxxxxxx).
        songs_folder: Folder where the manifest file will be saved.

    Returns:
        Tuple of (success, manifest_path, error_message).
    """
    manifest_path = songs_folder / MANIFEST_FILENAME
    playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"

    print(f"Fetching playlist manifest from: {playlist_url}")

    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--flat-playlist",
                "--print", "%(playlist_index)s;;;%(id)s;;;%(title)s",
                playlist_url,
            ],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )

        if result.returncode != 0:
            return False, manifest_path, result.stderr.strip() or "Unknown error"

        # Write manifest to file
        manifest_content = result.stdout
        if not manifest_content.strip():
            return False, manifest_path, "Empty playlist or no videos found"

        manifest_path.write_text(manifest_content, encoding="utf-8-sig")
        return True, manifest_path, ""

    except subprocess.TimeoutExpired:
        return False, manifest_path, "Manifest fetch timed out after 5 minutes"
    except Exception as e:
        return False, manifest_path, str(e)


def download_song(entry: ManifestEntry, songs_folder: Path) -> tuple[bool, str]:
    """
    Downloads a single song using yt-dlp.

    Returns:
        Tuple of (success, error_message).
    """
    sanitized_title = sanitize_filename(entry.title)
    output_template = str(songs_folder / f"{entry.index_str} - {sanitized_title}.%(ext)s")
    video_url = f"https://www.youtube.com/watch?v={entry.video_id}"

    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "-x",  # Extract audio
                "-o", output_template,
                video_url,
            ],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )

        if result.returncode == 0:
            return True, ""
        else:
            return False, result.stderr.strip() or "Unknown error"

    except subprocess.TimeoutExpired:
        return False, "Download timed out after 5 minutes"
    except Exception as e:
        return False, str(e)


def download_missing_songs(
    songs_folder: Path,
    manifest: dict[int, ManifestEntry],
    existing_indices: set[int],
    output_path: Path,
    dry_run: bool = False,
    start_offset: int = 0,
) -> DownloadReport:
    """Downloads all missing songs, writing reports incrementally."""
    report = DownloadReport()
    missing_entries = [e for idx, e in sorted(manifest.items()) if idx not in existing_indices]

    # TODO: Remove this limit after testing
    # missing_entries = missing_entries[:100]

    # Mark existing as already_exists
    for idx in existing_indices:
        if idx in manifest:
            report.already_exists.append(manifest[idx])

    if not missing_entries:
        print("No missing songs to download.")
        return report

    # Rotate the list to start from the offset, then loop back
    if start_offset > 0 and len(missing_entries) > 1:
        effective_offset = start_offset % len(missing_entries)
        missing_entries = missing_entries[effective_offset:] + missing_entries[:effective_offset]
        print(f"Starting from offset {effective_offset} (rotated order)")

    print(f"Found {len(missing_entries)} missing songs to download.")

    # Clear the failed manifest file at the start
    if not dry_run:
        clear_failed_manifest(output_path)

    for i, entry in enumerate(missing_entries):
        print(f"\n[{i + 1}/{len(missing_entries)}] {entry.index_str} - {entry.title}")

        if dry_run:
            print("  [DRY RUN] Would download")
            report.skipped_dry_run.append(entry)
            continue

        success, error = download_song(entry, songs_folder)

        if success:
            print("  Downloaded successfully")
            report.downloaded.append(entry)
        else:
            print(f"  FAILED: {error}")
            report.failed.append((entry, error))
            append_failed_entry(entry, output_path)

        # Write report after each download attempt
        write_report(report, output_path, dry_run)

        # Rate limiting delay (skip after last download)
        if i < len(missing_entries) - 1:
            print(f"  Waiting {DOWNLOAD_DELAY_SECONDS}s before next download...")
            time.sleep(DOWNLOAD_DELAY_SECONDS)

    return report


def get_failed_manifest_path(output_path: Path) -> Path:
    """Returns the path for the failed downloads manifest file."""
    return output_path.parent / "failed_downloads.txt"


def clear_failed_manifest(output_path: Path) -> None:
    """Clears/creates the failed downloads manifest file."""
    failed_path = get_failed_manifest_path(output_path)
    failed_path.write_text("", encoding="utf-8-sig")


def append_failed_entry(entry: ManifestEntry, output_path: Path) -> None:
    """Appends a single failed entry to the failed downloads manifest."""
    failed_path = get_failed_manifest_path(output_path)
    line = f"{entry.index_str};;;{entry.video_id};;;{entry.title}\n"
    with open(failed_path, "a", encoding="utf-8-sig") as f:
        f.write(line)


def write_report(report: DownloadReport, output_path: Path, dry_run: bool) -> None:
    """Writes the download report to a text file."""
    lines: list[str] = []

    lines.append("=" * 60)
    lines.append("SONG DOWNLOADER REPORT")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if dry_run:
        lines.append("MODE: DRY RUN (no actual downloads)")
    lines.append("=" * 60)
    lines.append("")

    lines.append("SUMMARY")
    lines.append("-" * 40)
    lines.append(f"Downloaded:      {len(report.downloaded)}")
    lines.append(f"Already existed: {len(report.already_exists)}")
    lines.append(f"Failed:          {len(report.failed)}")
    if dry_run:
        lines.append(f"Would download:  {len(report.skipped_dry_run)}")
    lines.append("")

    if report.downloaded:
        lines.append("DOWNLOADED")
        lines.append("-" * 40)
        for entry in report.downloaded:
            lines.append(f"  [{entry.index_str}] {entry.title}")
        lines.append("")

    if report.failed:
        lines.append("FAILED")
        lines.append("-" * 40)
        for entry, error in report.failed:
            lines.append(f"  [{entry.index_str}] {entry.title}")
            lines.append(f"           Video ID: {entry.video_id}")
            lines.append(f"           Error: {error}")
        lines.append("")

    if dry_run and report.skipped_dry_run:
        lines.append("WOULD DOWNLOAD (dry run)")
        lines.append("-" * 40)
        for entry in report.skipped_dry_run:
            lines.append(f"  [{entry.index_str}] {entry.title}")
            lines.append(f"           Video ID: {entry.video_id}")
        lines.append("")

    lines.append("=" * 60)
    lines.append("END OF REPORT")
    lines.append("=" * 60)

    output_path.write_text("\n".join(lines), encoding="utf-8-sig")
    print(f"\nReport written to: {output_path}")


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Download missing songs from a YouTube playlist.",
        epilog="Either provide a playlist_id or use --use-manifest with an existing file.",
    )

    parser.add_argument(
        "songs_folder",
        type=Path,
        help="Path to the folder containing song files",
    )

    parser.add_argument(
        "playlist_id",
        type=str,
        nargs="?",
        default=None,
        help="YouTube playlist ID (e.g., PLxxxxxxxx)",
    )

    parser.add_argument(
        "--use-manifest",
        type=Path,
        default=None,
        metavar="FILE",
        help="Use an existing manifest file instead of fetching from YouTube",
    )

    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Path for the output report file (default: <songs_folder>/download_report.txt)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded without actually downloading",
    )

    parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="Skip ahead N songs in the queue, then loop back to process skipped ones",
    )

    args = parser.parse_args()

    # Validate: must have either playlist_id or --use-manifest, but not both
    if args.playlist_id and args.use_manifest:
        print("Error: Cannot specify both playlist_id and --use-manifest")
        return 1

    if not args.playlist_id and not args.use_manifest:
        print("Error: Must provide either a playlist_id or --use-manifest")
        parser.print_usage()
        return 1

    # Validate paths
    if not args.songs_folder.is_dir():
        print(f"Error: Songs folder not found: {args.songs_folder}")
        return 1

    # Check yt-dlp
    if not check_ytdlp_installed():
        print("Error: yt-dlp is not installed or not in PATH")
        return 1

    # Determine manifest path
    if args.use_manifest:
        manifest_path = args.use_manifest
        if not manifest_path.is_file():
            print(f"Error: Manifest file not found: {manifest_path}")
            return 1
        print(f"Using existing manifest: {manifest_path}")
    else:
        # Download manifest from playlist
        success, manifest_path, error = download_playlist_manifest(
            args.playlist_id, args.songs_folder
        )
        if not success:
            print(f"Error: Failed to fetch playlist manifest: {error}")
            return 1
        print(f"Manifest saved to: {manifest_path}")

    output_path = args.output or (args.songs_folder / "download_report.txt")

    print(f"Songs folder: {args.songs_folder}")
    print(f"Manifest file: {manifest_path}")
    print(f"Dry run: {args.dry_run}")
    print("-" * 40)

    # Parse manifest
    try:
        manifest = parse_manifest(manifest_path)
        print(f"Loaded {len(manifest)} entries from manifest")
    except Exception as e:
        print(f"Error: Failed to parse manifest: {e}")
        return 1

    # Find existing songs
    existing_indices = find_existing_indices(args.songs_folder, manifest)
    print(f"Found {len(existing_indices)} existing songs")

    # Download missing songs (reports written incrementally)
    report = download_missing_songs(
        args.songs_folder,
        manifest,
        existing_indices,
        output_path,
        dry_run=args.dry_run,
        start_offset=args.start_offset,
    )

    # Final report write (for dry-run or when no downloads attempted)
    write_report(report, output_path, args.dry_run)

    # Print summary
    print("-" * 40)
    print(f"Downloaded: {len(report.downloaded)}")
    print(f"Failed: {len(report.failed)}")

    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())

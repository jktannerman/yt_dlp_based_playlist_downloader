"""
Alternate Song Downloader

Downloads alternate versions of songs that failed to download from their original video IDs.
Uses yt-dlp to download audio; titles are extracted from the resulting filenames.

Expected input format per line:
0001;;;VIDEO_ID
0001;;;VIDEO_ID;;;optional comment (ignored)

This is a recovery/replacement tool for use after the main song_downloader.py has run.
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

# Encodings to try when reading the alternates file
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
DOWNLOAD_DELAY_SECONDS: int = 4


@dataclass
class AlternateEntry:
    """Represents a single entry from the alternates file."""
    index: int
    video_id: str
    title: str | None = None  # Populated after download

    @property
    def index_str(self) -> str:
        """Returns the 4-digit zero-padded index string."""
        return f"{self.index:04d}"


@dataclass
class DownloadReport:
    """Collects all download outcomes for the final report."""
    downloaded: list[AlternateEntry] = field(default_factory=list)
    already_exists: list[AlternateEntry] = field(default_factory=list)
    failed: list[tuple[AlternateEntry, str]] = field(default_factory=list)
    skipped_dry_run: list[AlternateEntry] = field(default_factory=list)


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


def validate_alternates_content(content: str) -> bool:
    """Validates that content looks like a valid alternates file."""
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        # Expect format: NNNN;;;video_id (2 fields)
        if re.match(r"^\d{4};;;", line):
            return True
    return False


def read_alternates_with_encoding(alternates_path: Path) -> tuple[str, str]:
    """Attempts to read the alternates file with multiple encodings."""
    for encoding in ENCODINGS_TO_TRY:
        try:
            content = alternates_path.read_text(encoding=encoding)
            if content and validate_alternates_content(content):
                return content, encoding
        except (UnicodeDecodeError, UnicodeError):
            continue

    raise ValueError(
        "Could not read alternates file with any supported encoding"
    )


def parse_alternates(alternates_path: Path) -> list[AlternateEntry]:
    """Parses the alternates file."""
    content, encoding_used = read_alternates_with_encoding(alternates_path)
    print(f"Read alternates file using encoding: {encoding_used}")

    entries: list[AlternateEntry] = []

    for line_num, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        parts = line.split(";;;")
        if len(parts) < 2:
            print(f"Warning: Skipping malformed line {line_num}: {line[:50]}...")
            print(f"         Expected format: NNNN;;;video_id[;;;comment]")
            continue

        try:
            index = int(parts[0])
            video_id = parts[1].strip()
            entries.append(AlternateEntry(index=index, video_id=video_id))
        except ValueError as e:
            print(f"Warning: Could not parse line {line_num}: {e}")
            continue

    return entries


def find_existing_indices(songs_folder: Path) -> set[int]:
    """Finds which indices already have files in the folder."""
    existing_indices: set[int] = set()
    media_files = [f for f in songs_folder.iterdir() if f.is_file() and is_media_file(f)]

    for file_path in media_files:
        file_index = get_file_index(file_path.name)
        if file_index is not None:
            existing_indices.add(file_index)

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


def find_downloaded_file(songs_folder: Path, index: int) -> Path | None:
    """Finds the most recently modified file matching the given index."""
    pattern = f"{index:04d} - *"
    matches = list(songs_folder.glob(pattern))
    media_matches = [f for f in matches if f.is_file() and is_media_file(f)]

    if not media_matches:
        return None

    # Return most recently modified if multiple matches
    return max(media_matches, key=lambda f: f.stat().st_mtime)


def extract_title_from_filename(file_path: Path) -> str | None:
    """Extracts the title portion from a filename like '0001 - Title.ext'."""
    stem = file_path.stem
    match = re.match(r"^\d{4}\s*-\s*(.+)$", stem)
    if match:
        return match.group(1)
    return None


def download_song(entry: AlternateEntry, songs_folder: Path) -> tuple[bool, str]:
    """
    Downloads a single song using yt-dlp.

    Uses yt-dlp's built-in title sanitization via the %(title)s template.

    Returns:
        Tuple of (success, error_message).
    """
    # Let yt-dlp handle title sanitization
    output_template = str(songs_folder / f"{entry.index_str} - %(title)s.%(ext)s")
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


def download_alternates(
    songs_folder: Path,
    entries: list[AlternateEntry],
    existing_indices: set[int],
    output_path: Path,
    dry_run: bool = False,
) -> DownloadReport:
    """Downloads alternate songs, writing reports incrementally."""
    report = DownloadReport()

    if not entries:
        print("No entries to process.")
        return report

    print(f"Processing {len(entries)} alternate entries.")

    for i, entry in enumerate(entries):
        print(f"\n[{i + 1}/{len(entries)}] Index: {entry.index_str}, Video ID: {entry.video_id}")

        # Safety check: skip if file already exists
        if entry.index in existing_indices:
            print(f"  SKIPPED: File with index {entry.index_str} already exists")
            report.already_exists.append(entry)
            write_report(report, output_path, dry_run)
            continue

        if dry_run:
            print(f"  [DRY RUN] Would download from: https://www.youtube.com/watch?v={entry.video_id}")
            report.skipped_dry_run.append(entry)
            write_report(report, output_path, dry_run)
            continue

        # Download the song
        print(f"  Downloading...")
        success, error = download_song(entry, songs_folder)

        if success:
            # Extract title from the downloaded filename
            downloaded_file = find_downloaded_file(songs_folder, entry.index)
            if downloaded_file:
                entry.title = extract_title_from_filename(downloaded_file)
                print(f"  Downloaded: {entry.title or downloaded_file.name}")
            else:
                print(f"  Downloaded successfully (could not locate file)")
            report.downloaded.append(entry)
            # Update existing indices so we don't try to overwrite
            existing_indices.add(entry.index)
        else:
            print(f"  FAILED: {error}")
            report.failed.append((entry, error))

        # Write report after each attempt
        write_report(report, output_path, dry_run)

        # Rate limiting delay (skip after last download)
        if i < len(entries) - 1 and not dry_run:
            print(f"  Waiting {DOWNLOAD_DELAY_SECONDS}s before next download...")
            time.sleep(DOWNLOAD_DELAY_SECONDS)

    return report


def write_report(report: DownloadReport, output_path: Path, dry_run: bool) -> None:
    """Writes the download report to a text file."""
    lines: list[str] = []

    lines.append("=" * 60)
    lines.append("ALTERNATE DOWNLOADER REPORT")
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
            title_display = entry.title or "(title unknown)"
            lines.append(f"  [{entry.index_str}] {title_display}")
            lines.append(f"           Video ID: {entry.video_id}")
        lines.append("")

    if report.already_exists:
        lines.append("SKIPPED (already exists)")
        lines.append("-" * 40)
        for entry in report.already_exists:
            lines.append(f"  [{entry.index_str}] Video ID: {entry.video_id}")
        lines.append("")

    if report.failed:
        lines.append("FAILED")
        lines.append("-" * 40)
        for entry, error in report.failed:
            title_display = entry.title or "(title unknown)"
            lines.append(f"  [{entry.index_str}] {title_display}")
            lines.append(f"           Video ID: {entry.video_id}")
            lines.append(f"           Error: {error}")
        lines.append("")

    if dry_run and report.skipped_dry_run:
        lines.append("WOULD DOWNLOAD (dry run)")
        lines.append("-" * 40)
        for entry in report.skipped_dry_run:
            lines.append(f"  [{entry.index_str}] Video ID: {entry.video_id}")
        lines.append("")

    lines.append("=" * 60)
    lines.append("END OF REPORT")
    lines.append("=" * 60)

    output_path.write_text("\n".join(lines), encoding="utf-8-sig")


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Download alternate versions of songs from YouTube.",
        epilog="Input file format: NNNN;;;video_id[;;;comment] (one per line)",
    )

    parser.add_argument(
        "songs_folder",
        type=Path,
        help="Path to the folder where songs will be downloaded",
    )

    parser.add_argument(
        "alternates_file",
        type=Path,
        help="Path to the alternates file (index;;;video_id format)",
    )

    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Path for the output report file (default: <songs_folder>/alternate_report.txt)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded without actually downloading",
    )

    args = parser.parse_args()

    # Validate paths
    if not args.songs_folder.is_dir():
        print(f"Error: Songs folder not found: {args.songs_folder}")
        return 1

    if not args.alternates_file.is_file():
        print(f"Error: Alternates file not found: {args.alternates_file}")
        return 1

    # Check yt-dlp
    if not check_ytdlp_installed():
        print("Error: yt-dlp is not installed or not in PATH")
        return 1

    output_path = args.output or (args.songs_folder / "alternate_report.txt")

    print(f"Songs folder: {args.songs_folder}")
    print(f"Alternates file: {args.alternates_file}")
    print(f"Output report: {output_path}")
    print(f"Dry run: {args.dry_run}")
    print("-" * 40)

    # Parse alternates file
    try:
        entries = parse_alternates(args.alternates_file)
        print(f"Loaded {len(entries)} entries from alternates file")
    except Exception as e:
        print(f"Error: Failed to parse alternates file: {e}")
        return 1

    if not entries:
        print("No valid entries found in alternates file.")
        return 0

    # Find existing files
    existing_indices = find_existing_indices(args.songs_folder)
    print(f"Found {len(existing_indices)} existing files in songs folder")

    # Download alternates
    report = download_alternates(
        args.songs_folder,
        entries,
        existing_indices,
        output_path,
        dry_run=args.dry_run,
    )

    # Final report write
    write_report(report, output_path, args.dry_run)
    print(f"\nReport written to: {output_path}")

    # Print summary
    print("-" * 40)
    print(f"Downloaded: {len(report.downloaded)}")
    print(f"Skipped (exists): {len(report.already_exists)}")
    print(f"Failed: {len(report.failed)}")

    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())

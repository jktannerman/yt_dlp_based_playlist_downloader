"""
Song Downloader

Downloads missing songs from YouTube based on a playlist.
First fetches the playlist manifest using yt-dlp, then downloads missing songs.

Usage:
    py -3.13 song_downloader.py <songs_folder> <playlist_id_or_url>
    py -3.13 song_downloader.py <songs_folder> --use-manifest <manifest_file>

Expected manifest format per line:
#COLUMNS: index;;;video_id;;;title;;;view_count
0001;;;VIDEO_ID;;;Song Title;;;12345
"""

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from manifest_common import (
    FULL_COLUMNS,
    ManifestEntry,
    extract_title_from_filename,
    find_matching_entry,
    format_manifest_header,
    format_manifest_line,
    get_file_index,
    is_media_file,
    normalize_title,
    parse_manifest,
)

# Download settings
DOWNLOAD_DELAY_SECONDS: int = 4

# Default manifest filename
MANIFEST_FILENAME: str = "playlist_manifest.txt"


def parse_playlist_input(raw: str) -> tuple[str, bool]:
    """Parses a playlist ID or full YouTube/YTM URL.

    Args:
        raw: A raw playlist ID (e.g., PLxxxxxxxx) or a full URL
            (e.g., https://www.youtube.com/playlist?list=PLxxxxxxxx
            or https://music.youtube.com/playlist?list=PLxxxxxxxx).

    Returns:
        Tuple of (playlist_id, is_ytm). is_ytm is True when the input
        is a YouTube Music URL.
    """
    parsed = urlparse(raw)
    if parsed.hostname and "youtube" in parsed.hostname:
        is_ytm = "music.youtube.com" in parsed.hostname
        list_param = parse_qs(parsed.query).get("list")
        if list_param:
            return list_param[0], is_ytm
    return raw, False


@dataclass
class DownloadReport:
    """Collects all download outcomes for the final report."""
    downloaded: list[ManifestEntry] = field(default_factory=list)
    already_exists: list[ManifestEntry] = field(default_factory=list)
    failed: list[tuple[ManifestEntry, str]] = field(default_factory=list)
    skipped_dry_run: list[ManifestEntry] = field(default_factory=list)


def sanitize_filename(title: str) -> str:
    """Sanitizes a title for use in Windows filenames."""
    invalid_chars = '<>:"/\\|?*'
    sanitized = title
    for char in invalid_chars:
        sanitized = sanitized.replace(char, '-')
    return sanitized


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


def download_playlist_manifest(
    playlist_id: str, songs_folder: Path, is_ytm: bool = False
) -> tuple[bool, Path, str]:
    """Downloads the playlist manifest using yt-dlp.

    Args:
        playlist_id: YouTube playlist ID (e.g., PLxxxxxx).
        songs_folder: Folder where the manifest file will be saved.
        is_ytm: If True, use music.youtube.com as the source.

    Returns:
        Tuple of (success, manifest_path, error_message).
    """
    manifest_path = songs_folder / MANIFEST_FILENAME
    base = "https://music.youtube.com" if is_ytm else "https://www.youtube.com"
    playlist_url = f"{base}/playlist?list={playlist_id}"

    print(f"Fetching playlist manifest from: {playlist_url}")

    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--flat-playlist",
                "--print", "%(playlist_index)s;;;%(id)s;;;%(title)s;;;%(view_count)s",
                playlist_url,
            ],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )

        if result.returncode != 0:
            return False, manifest_path, result.stderr.strip() or "Unknown error"

        # Write manifest to file with header
        manifest_content = result.stdout
        if not manifest_content.strip():
            return False, manifest_path, "Empty playlist or no videos found"

        header = format_manifest_header(FULL_COLUMNS)
        manifest_path.write_text(
            header + "\n" + manifest_content, encoding="utf-8-sig"
        )
        return True, manifest_path, ""

    except subprocess.TimeoutExpired:
        return False, manifest_path, "Manifest fetch timed out after 5 minutes"
    except Exception as e:
        return False, manifest_path, str(e)


def download_song(
    entry: ManifestEntry, songs_folder: Path, is_ytm: bool = False
) -> tuple[bool, str]:
    """Downloads a single song using yt-dlp.

    Args:
        entry: The manifest entry for the song.
        songs_folder: Folder where the song will be saved.
        is_ytm: If True, use music.youtube.com as the source.

    Returns:
        Tuple of (success, error_message).
    """
    sanitized_title = sanitize_filename(entry.title)
    output_template = str(songs_folder / f"{entry.index_str} - {sanitized_title}.%(ext)s")
    base = "https://music.youtube.com" if is_ytm else "https://www.youtube.com"
    video_url = f"{base}/watch?v={entry.video_id}"

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
    start_index: int = 0,
    limit: int | None = None,
    sort_by_views: bool = False,
    is_ytm: bool = False,
) -> DownloadReport:
    """Downloads all missing songs, writing reports incrementally."""
    report = DownloadReport()
    missing_entries = [e for idx, e in sorted(manifest.items()) if idx not in existing_indices]

    # Mark existing as already_exists
    for idx in existing_indices:
        if idx in manifest:
            report.already_exists.append(manifest[idx])

    if not missing_entries:
        print("No missing songs to download.")
        return report

    if sort_by_views:
        # Sort descending by view count; None-view-count entries go last
        missing_entries.sort(
            key=lambda e: (e.view_count is not None, e.view_count or 0),
            reverse=True,
        )
        if start_index > 0:
            print("Warning: --start-index is ignored when --sort-by-views is active")

        # Apply limit before re-indexing
        if limit is not None and limit > 0:
            missing_entries = missing_entries[:limit]

        # Re-index by popularity rank (1-based)
        for rank, entry in enumerate(missing_entries, start=1):
            entry.index = rank
    else:
        # Rotate the list to start from the given playlist index, then loop back
        if start_index > 0 and len(missing_entries) > 1:
            rotate_pos = None
            for i, entry in enumerate(missing_entries):
                if entry.index >= start_index:
                    rotate_pos = i
                    break

            if rotate_pos is None:
                print(f"No missing entries with index >= {start_index}, starting from index {missing_entries[0].index}")
            elif rotate_pos > 0:
                missing_entries = missing_entries[rotate_pos:] + missing_entries[:rotate_pos]
                print(f"Starting from playlist index {missing_entries[0].index} (rotated order)")
            else:
                print(f"Starting from playlist index {missing_entries[0].index}")

        if limit is not None and limit > 0:
            missing_entries = missing_entries[:limit]

    print(f"Found {len(missing_entries)} missing songs to download."
          + (f" (limited to {limit})" if limit is not None and limit > 0 else ""))

    # Clear the failed manifest file at the start
    if not dry_run:
        clear_failed_manifest(output_path)

    for i, entry in enumerate(missing_entries):
        views_suffix = ""
        if sort_by_views and entry.view_count is not None:
            views_suffix = f" (views: {entry.view_count:,})"
        print(f"\n[{i + 1}/{len(missing_entries)}] {entry.index_str} - {entry.title}{views_suffix}")

        if dry_run:
            if sort_by_views and entry.view_count is not None:
                print(f"  [DRY RUN] Would download (views: {entry.view_count:,})")
            else:
                print("  [DRY RUN] Would download")
            report.skipped_dry_run.append(entry)
            continue

        success, error = download_song(entry, songs_folder, is_ytm=is_ytm)

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
    """Clears/creates the failed downloads manifest file with header."""
    failed_path = get_failed_manifest_path(output_path)
    header = format_manifest_header(FULL_COLUMNS)
    failed_path.write_text(header + "\n", encoding="utf-8-sig")


def append_failed_entry(entry: ManifestEntry, output_path: Path) -> None:
    """Appends a single failed entry to the failed downloads manifest."""
    failed_path = get_failed_manifest_path(output_path)
    line = format_manifest_line(entry, FULL_COLUMNS) + "\n"
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
        help="YouTube playlist ID or full URL (e.g., PLxxxxxxxx, "
             "https://www.youtube.com/playlist?list=PLxxxxxxxx, or "
             "https://music.youtube.com/playlist?list=PLxxxxxxxx). "
             "When a YouTube Music URL is provided, downloads use music.youtube.com.",
    )

    parser.add_argument(
        "--use-manifest",
        type=Path,
        nargs="?",
        default=None,
        const=True,
        metavar="FILE",
        help="Use an existing manifest file instead of fetching from YouTube. "
             "If no path is given, looks for the default manifest in the songs folder.",
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
        "--start-index",
        type=int,
        default=0,
        help="Start downloading from this playlist index, then loop back to earlier ones",
    )

    parser.add_argument(
        "-n", "--limit",
        type=int,
        default=None,
        help="Maximum number of songs to download (default: all)",
    )

    parser.add_argument(
        "--sort-by-views",
        action="store_true",
        help="Sort by view count (descending) before downloading. "
             "Combined with --limit, downloads only the N most popular songs.",
    )

    args = parser.parse_args()

    # Parse playlist input (extract ID and detect YTM)
    is_ytm = False
    if args.playlist_id:
        args.playlist_id, is_ytm = parse_playlist_input(args.playlist_id)

    # Resolve --use-manifest when given without a path
    if args.use_manifest is True:
        args.use_manifest = args.songs_folder / MANIFEST_FILENAME

    # Validate: must have either playlist_id or --use-manifest
    if args.playlist_id and args.use_manifest:
        print("Warning: --use-manifest overrides playlist_id; ignoring playlist_id")

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
            args.playlist_id, args.songs_folder, is_ytm=is_ytm
        )
        if not success:
            print(f"Error: Failed to fetch playlist manifest: {error}")
            return 1
        print(f"Manifest saved to: {manifest_path}")

    output_path = args.output or (args.songs_folder / "download_report.txt")

    print(f"Songs folder: {args.songs_folder}")
    print(f"Manifest file: {manifest_path}")
    print(f"Dry run: {args.dry_run}")
    if args.sort_by_views:
        print(f"Sort by views: enabled")
    print("-" * 40)

    # Parse manifest
    try:
        manifest = parse_manifest(manifest_path)
        print(f"Loaded {len(manifest)} entries from manifest")
    except Exception as e:
        print(f"Error: Failed to parse manifest: {e}")
        return 1

    # Warn if sort-by-views but no view data
    if args.sort_by_views:
        has_view_data = any(e.view_count is not None for e in manifest.values())
        if not has_view_data:
            print("Warning: --sort-by-views is set but no entries have view count data.")
            print("         Songs will be sorted with all entries treated equally.")

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
        start_index=args.start_index,
        limit=args.limit,
        sort_by_views=args.sort_by_views,
        is_ytm=is_ytm,
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

"""
Playlist Auditor

Compares song files in a folder against a manifest file and reports
discrepancies (missing and unexpected songs). This is a read-only version
of the playlist_fixer.py script.

Expected manifest format per line:
0001;;;VIDEO_ID;;;Title

Also supports the new header-based format:
#COLUMNS: index;;;video_id;;;title;;;view_count

Expected filename format: %(playlist_index)04d - %(title)s.%(ext)s
Example: 0001 - Flight [Monstercat Release].mp3
"""

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from manifest_common import (
    ManifestEntry,
    extract_title_from_filename,
    find_matching_entry,
    get_file_index,
    is_media_file,
    normalize_title,
    parse_manifest,
)


@dataclass
class AuditReport:
    """Collects all discrepancies found during the audit."""
    missing_songs: list[ManifestEntry] = field(default_factory=list)
    unexpected_songs: list[str] = field(default_factory=list)
    matched_songs: list[tuple[str, ManifestEntry]] = field(default_factory=list)

    def has_issues(self) -> bool:
        """Returns True if there are any discrepancies."""
        return bool(self.missing_songs or self.unexpected_songs)


def audit_playlist_files(
    songs_folder: Path,
    manifest_path: Path,
) -> AuditReport:
    """Audits playlist files against a manifest (read-only).

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
    """Prints the audit report to stdout.

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
    """Writes the audit report to a text file.

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

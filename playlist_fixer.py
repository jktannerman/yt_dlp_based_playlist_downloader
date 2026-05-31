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
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
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
    sanitize_filename,
)


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


def safe_rename_file(src: Path, dst: Path, timeout: float = 5.0) -> None:
    """Safely rename a file using copy-then-delete strategy with timeout.

    This approach is more robust on Windows where Path.rename() can hang
    indefinitely when files are locked by indexers, antivirus, etc.

    Args:
        src: Source file path.
        dst: Destination file path.
        timeout: Maximum seconds to wait for the operation (default: 5).

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
    """Safely delete a file with timeout protection.

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


def build_manifest_by_title(
    manifest: dict[int, ManifestEntry],
) -> dict[str, ManifestEntry]:
    """Builds a lookup dictionary from normalized title to manifest entry.

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
    """Main function to fix playlist file names.

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

    # Step 3a: Classify all files and collect pending renames.
    # A rename is (current_file_path, source_idx, target_idx, new_filename).
    # We gather everything first so we can resolve chains in dependency order.
    RenameTask = tuple[Path, int | None, int, str]
    pending: list[RenameTask] = []

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
        manifest_entry = manifest_by_title.get(norm_title)

        if not manifest_entry:
            report.unmatched_files.append(filename)
            continue

        expected_index = manifest_entry.index

        if file_index == expected_index:
            report.already_correct.append(filename)
            continue

        # Build sanitized new filename
        new_filename = f"{expected_index:04d} - {sanitize_filename(manifest_entry.title)}{file_path.suffix}"
        pending.append((file_path, file_index, expected_index, new_filename))

    # Step 3b: Execute renames in dependency order.
    #
    # A rename (A -> B) is blocked if B is currently the source of another pending
    # rename — we must move that file first. Iterating until no renames remain
    # handles arbitrarily long chains. If a full pass makes no progress, the
    # remaining renames form a cycle; we break it by staging one file under a
    # temporary index beyond all occupied slots.
    pending_sources: set[int] = {src for _, src, _, _ in pending if src is not None}

    # Track live file paths so cycle-breaking temp renames update subsequent tasks.
    # Index in `pending` -> current Path (may change after a temp rename).
    live_paths: dict[int, Path] = {i: fp for i, (fp, *_) in enumerate(pending)}

    remaining_indices: set[int] = set(range(len(pending)))
    all_occupied: set[int] = set(files_by_index.keys())

    max_passes = len(pending) + 1
    for _pass in range(max_passes):
        if not remaining_indices:
            break

        made_progress = False
        for i in list(remaining_indices):
            _, source_idx, target_idx, new_filename = pending[i]
            file_path = live_paths[i]

            # Blocked if another pending rename still sits at our target slot.
            if target_idx in pending_sources:
                continue

            new_path = file_path.parent / new_filename
            if not dry_run:
                try:
                    print(f"  Renaming: {file_path.name}")
                    print(f"       -> {new_filename}")
                    safe_rename_file(file_path, new_path)
                    report.renamed_files.append((pending[i][0].name, new_filename))
                except (OSError, FileExistsError, TimeoutError) as e:
                    report.errors.append(f"Failed to rename '{file_path.name}': {e}")
            else:
                report.renamed_files.append((pending[i][0].name, new_filename))

            if source_idx is not None:
                pending_sources.discard(source_idx)
            remaining_indices.discard(i)
            made_progress = True

        if not made_progress and remaining_indices:
            # All remaining renames are in a cycle. Break one by moving its
            # source file to a temporary slot beyond the highest occupied index.
            i = next(iter(remaining_indices))
            _, source_idx, target_idx, new_filename = pending[i]
            file_path = live_paths[i]

            temp_idx = max(all_occupied | pending_sources, default=0) + 1
            temp_filename = f"{temp_idx:04d} - _TEMP_{file_path.stem}{file_path.suffix}"
            temp_path = file_path.parent / temp_filename

            if not dry_run:
                try:
                    safe_rename_file(file_path, temp_path)
                    live_paths[i] = temp_path
                except (OSError, FileExistsError, TimeoutError) as e:
                    report.errors.append(
                        f"Failed cycle-break temp rename for '{file_path.name}': {e}"
                    )
                    remaining_indices.discard(i)
                    if source_idx is not None:
                        pending_sources.discard(source_idx)
                    continue
            else:
                live_paths[i] = temp_path

            if source_idx is not None:
                pending_sources.discard(source_idx)
            pending_sources.add(temp_idx)
            all_occupied.add(temp_idx)
            # Update pending entry so next pass uses temp as source
            pending[i] = (temp_path, temp_idx, target_idx, new_filename)

    for i in remaining_indices:
        _, source_idx, target_idx, new_filename = pending[i]
        report.errors.append(
            f"Cannot rename '{live_paths[i].name}' to index {target_idx:04d}: unresolved conflict"
        )

    print(f"  Files to rename: {len(report.renamed_files)}")
    print(f"  Already correct: {len(report.already_correct)}")
    print(f"  Unmatched files: {len(report.unmatched_files)}")

    return report


def write_report(report: FixerReport, output_path: Path, dry_run: bool) -> None:
    """Writes the fix report to a text file.

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

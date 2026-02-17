# Playlist Downloader & Fixer

Tools for downloading songs from YouTube playlists and maintaining playlist file integrity using yt-dlp.

## Requirements

- Python 3.13+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) installed and available in PATH

## Programs

### manifest_common.py

Shared library used by all other scripts. Contains:
- `ManifestEntry` dataclass (with optional `view_count` field)
- Manifest parsing with header-based and legacy format support
- Title normalization (NFKD + ASCII-only + regex)
- File utility functions (`is_media_file`, `get_file_index`, `extract_title_from_filename`)
- Constants (`MEDIA_EXTENSIONS`, `ENCODINGS_TO_TRY`, `MANIFEST_DELIMITER`)

This is not a standalone tool — it is imported by the other scripts.

---

### song_downloader.py

Downloads missing songs from a YouTube playlist.

**Usage:**
```
py -3.13 song_downloader.py <songs_folder> <playlist_id> [options]
py -3.13 song_downloader.py <songs_folder> --use-manifest <manifest_file> [options]
```

**Arguments:**
| Argument | Description |
|----------|-------------|
| `songs_folder` | Folder where songs are/will be stored |
| `playlist_id` | YouTube playlist ID (e.g., `PLxxxxxxxx`) |
| `--use-manifest [FILE]` | Use existing manifest instead of fetching from YouTube. If no path is given, looks for `playlist_manifest.txt` in the songs folder |
| `-o, --output FILE` | Report output path (default: `<songs_folder>/download_report.txt`) |
| `--dry-run` | Show what would be downloaded without downloading |
| `--start-index N` | Start from playlist index N, then loop back to earlier entries |
| `-n, --limit N` | Maximum number of songs to download (default: all) |
| `--sort-by-views` | Sort by view count (descending) before downloading |

**Features:**
- Fetches playlist manifest from YouTube or uses existing file
- Detects existing songs by index prefix or normalized title matching
- Downloads audio using yt-dlp with `-x` (extract audio)
- Incremental reporting (report updates after each download)
- Creates `failed_downloads.txt` listing failed entries for recovery
- Rate limiting between downloads (4 second delay)
- Handles multiple file encodings (UTF-8, UTF-16, CP1252, etc.)
- Tracks view counts in manifest for popularity-based sorting

**`--sort-by-views` flag:**
- Sorts missing songs by view count (highest first) before downloading
- Combined with `--limit N`, downloads only the N most popular missing songs
- Re-indexes files by popularity rank: most popular = `0001`, second = `0002`, etc. (original playlist indices are replaced)
- Entries without view data sort to the bottom
- Overrides `--start-index` with a warning (start-index is about playlist ordering, which doesn't apply when sorting by popularity)
- If the manifest has no view count data, a warning is printed

**Output files:**
- `download_report.txt` - Summary of downloaded, existing, and failed songs
- `failed_downloads.txt` - Manifest-format list of failed downloads (with header)
- `playlist_manifest.txt` - Saved playlist manifest (when fetched from YouTube)

---

### alternate_downloader.py

Downloads replacement songs using alternate video IDs. Use this after `song_downloader.py` to recover failed downloads with different source videos.

**Usage:**
```
py -3.13 alternate_downloader.py <songs_folder> <alternates_file> [options]
```

**Arguments:**
| Argument | Description |
|----------|-------------|
| `songs_folder` | Folder where songs will be downloaded |
| `alternates_file` | File with alternate video IDs to download |
| `-o, --output FILE` | Report output path (default: `<songs_folder>/alternate_report.txt`) |
| `--dry-run` | Show what would be downloaded without downloading |

**Features:**
- Downloads from alternate video IDs for specific playlist indices
- Skips indices that already have files (safety check)
- Extracts song title from downloaded filename (via yt-dlp)
- Incremental reporting
- Rate limiting between downloads (4 second delay)

**Alternates file format:**
```
0001;;;VIDEO_ID
0002;;;VIDEO_ID;;;optional comment (ignored)
```

---

### playlist_auditor.py

Audits song files against a manifest to find discrepancies (read-only). Useful for checking playlist integrity without making changes.

**Usage:**
```
py -3.13 playlist_auditor.py <songs_folder> <manifest_file> [options]
```

**Arguments:**
| Argument | Description |
|----------|-------------|
| `songs_folder` | Folder containing the song files |
| `manifest_file` | Path to the playlist manifest file |
| `-o, --output FILE` | Report output path (optional) |

**Features:**
- Compares files against manifest entries
- Matches by both index AND normalized title for accuracy
- Reports matched, missing, and unexpected songs
- Handles character encoding differences between filenames and manifest
- Unicode normalization (NFKD) for fullwidth character handling
- Strips non-ASCII characters during comparison to avoid false positives
- Converts all punctuation to spaces for robust matching

**Report categories:**
- **Matched songs** - Files that correctly match manifest entries
- **Missing songs** - Manifest entries with no corresponding file
- **Unexpected songs** - Files that don't match any manifest entry

---

### playlist_fixer.py

Fixes playlist files by deleting duplicates and correcting index offset errors.

**Usage:**
```
py -3.13 playlist_fixer.py <songs_folder> <manifest_file> [options]
```

**Arguments:**
| Argument | Description |
|----------|-------------|
| `songs_folder` | Folder containing the song files |
| `manifest_file` | Path to the playlist manifest file |
| `-o, --output FILE` | Report output path (default: `<songs_folder>/fix_report.txt`) |
| `--dry-run` | Show what would be done without making changes |

**Features:**

*Phase 1: Build Data Structures*
- Parses manifest into index-to-entry mapping
- Scans all media files and builds lookup tables by index and normalized title

*Phase 2: Delete Duplicates*
- Identifies files with the same normalized title
- Keeps the file with the lowest index
- Deletes all other duplicates
- Skips titles shorter than 3 characters (likely encoding artifacts)

*Phase 3: Fix Offset Errors*
- Matches each file to its correct manifest entry by title
- Renames files with wrong indices to their correct indices
- Only renames if the target index slot is empty (no conflicts)
- Tracks files that couldn't be matched to any manifest entry

**Title normalization:**
- Converts to lowercase
- Applies NFKD unicode normalization (handles fullwidth characters)
- Filters to ASCII-only (removes accented characters, special unicode)
- Converts all punctuation to spaces (handles `:` `"` `-` etc. uniformly)
- Collapses multiple spaces

**Report categories:**
- **Deleted duplicates** - Files removed as duplicates (with reason)
- **Renamed files** - Files renamed to correct index
- **Already correct** - Files that were already correctly named
- **Unmatched files** - Files with no matching manifest entry
- **Errors** - Any failures during processing

---

## Manifest Format

Manifests use the `;;;` (triple semicolon) delimiter. The new format includes a header line:

```
#COLUMNS: index;;;video_id;;;title;;;view_count
0001;;;dQw4w9WgXcQ;;;Never Gonna Give You Up;;;1500000000
0002;;;9bZkp7q19f0;;;Gangnam Style;;;5000000000
```

**Header line:** Starts with `#COLUMNS:` followed by the column names separated by `;;;`. This declares what fields are present and in what order.

**Legacy format (still fully supported):**
```
0001;;;VIDEO_ID;;;Song Title
0002;;;VIDEO_ID;;;Song Title
```

Manifests without a `#COLUMNS:` header are automatically parsed as legacy 3-field format (index, video_id, title). All entries from legacy manifests will have `view_count = None`.

**Fields:**
| Column | Description |
|--------|-------------|
| `index` | 4-digit zero-padded playlist index |
| `video_id` | YouTube video ID |
| `title` | Song title |
| `view_count` | View count (integer, or `NA` if unavailable) |

## File Naming Convention

Downloaded/fixed files follow the pattern:
```
NNNN - Song Title.ext
```
Where `NNNN` is the 4-digit zero-padded playlist index.

## Typical Workflow

1. **Download** - Run `song_downloader.py` to download songs from a playlist
2. **Review failures** - Check `failed_downloads.txt` for any failures
3. **Find alternates** - Locate alternate video IDs for failed songs
4. **Download alternates** - Run `alternate_downloader.py` with replacement IDs
5. **Audit** - Run `playlist_auditor.py` to check for discrepancies
6. **Fix** - Run `playlist_fixer.py` to clean up duplicates and fix misnamed files

**Popularity-based download:**
```
py -3.13 song_downloader.py ./songs PLxxxxxxxx --sort-by-views --limit 50
```
This downloads only the 50 most-viewed missing songs from the playlist.

## Encoding Support

All tools support multiple file encodings:
- UTF-8 (with and without BOM)
- UTF-16 (LE and BE)
- CP1252 (Windows Western European)
- ISO-8859-1 (Latin-1)

# Song Downloader - Implementation Plan

## Overview
A Python program that downloads missing songs from YouTube based on a manifest file.

## User Requirements (from `request_1.md`)
- Download missing songs that aren't present in the destination folder
- Use `yt-dlp` for downloading (audio only)
- Filename format: `NNNN - Title.ext` (4-digit zero-padded index)
- Download individually by video ID, not from playlist URL
- Implement rate limiting to avoid YouTube restrictions

## Decisions Made

| Setting | Choice | Rationale |
|---------|--------|-----------|
| Download delay | 15 seconds | Moderate balance of speed and safety |
| Audio format | Best available | Let yt-dlp choose native format (opus/m4a/etc) |
| Error handling | Skip and log | No retries, log failures to report |
| Dry-run mode | Yes | Show what would be downloaded without downloading |

## Core Functionality

1. **Parse manifest** using multi-encoding support from `playlist_fixer.py`
2. **Scan existing files** in destination folder, matching by index or normalized title
3. **Identify missing songs** by comparing manifest entries to existing files
4. **Download missing songs** individually via:
   ```
   yt-dlp -x -o "NNNN - Title.%(ext)s" https://youtube.com/watch?v=VIDEO_ID
   ```
5. **Wait 15 seconds** between each download
6. **Generate report** showing downloaded, skipped, and failed songs

## Reused Components from `playlist_fixer.py`

- `ENCODINGS_TO_TRY` - Multi-encoding manifest reading
- `MEDIA_EXTENSIONS` - Audio/video file extension detection
- `ManifestEntry` dataclass - Structured manifest entry representation
- `normalize_title()` - Title normalization for filename matching
- Windows filename sanitization (invalid chars + fullwidth Unicode)
- Report writing pattern with `utf-8-sig` encoding

## CLI Interface

```
py -3.13 song_downloader.py <songs_folder> <manifest_filename> [--dry-run] [-o report.txt]
```

### Arguments
| Argument | Required | Description |
|----------|----------|-------------|
| `songs_folder` | Yes | Path to folder containing existing songs |
| `manifest_filename` | Yes | Name of manifest file within songs_folder |
| `--dry-run` | No | Show what would be downloaded without downloading |
| `-o, --output` | No | Path for output report (default: `<songs_folder>/download_report.txt`) |

## Error Handling

- Verify `yt-dlp` is installed before starting
- Skip failed downloads and log them (no retries)
- Generate final report with all outcomes

## Manifest File Format

Each line contains three fields separated by `;;;`:
```
0001;;;VIDEO_ID;;;Song Title
0002;;;VIDEO_ID;;;Another Song
```

## Output Filename Format

```
{index:04d} - {sanitized_title}.{ext}
```

Example: `0001 - Flight [Monstercat Release].opus`

## Windows Filename Sanitization

Invalid characters replaced: `< > : " / \ | ? *`

Fullwidth Unicode equivalents also handled (e.g., `：` → `-`).

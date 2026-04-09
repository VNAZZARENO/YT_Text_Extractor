#!/usr/bin/env python3
"""Extract clean transcript text from a YouTube URL using yt-dlp."""

import argparse
import html
import json
import re
import sys
import tempfile
from pathlib import Path

from yt_dlp import YoutubeDL


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = SCRIPT_DIR / "transcripts"


def slugify(name: str, max_len: int = 80) -> str:
    """Turn an arbitrary title into a safe folder name."""
    # Replace filesystem-hostile chars with a space
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", name)
    # Collapse whitespace to single underscores
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    # Drop anything that isn't alnum, underscore, dash, dot
    cleaned = re.sub(r"[^\w.\-]", "", cleaned)
    cleaned = cleaned.strip("._-") or "untitled"
    return cleaned[:max_len]


def fetch_subtitles(url: str, lang: str, outdir: Path) -> tuple[dict, Path | None]:
    """Download subtitles (manual preferred, else auto) and return info + path."""
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [lang, f"{lang}.*", "en", "en.*"],
        "subtitlesformat": "vtt",
        "outtmpl": str(outdir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    video_id = info["id"]
    # Look for any matching VTT file in outdir
    candidates = sorted(outdir.glob(f"{video_id}*.vtt"))
    if not candidates:
        return info, None

    # Prefer manual subs over auto: yt-dlp names auto as .<lang>.vtt too,
    # but manual subs are written first if both available. Take the first.
    for c in candidates:
        if lang in c.name:
            return info, c
    return info, candidates[0]


def parse_vtt(path: Path) -> str:
    """Parse a WebVTT file into clean deduplicated plain text."""
    raw = path.read_text(encoding="utf-8", errors="replace")

    # Remove WEBVTT header and metadata blocks
    lines = raw.splitlines()
    text_lines: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("WEBVTT") or s.startswith("Kind:") or s.startswith("Language:"):
            continue
        # Skip cue timing lines
        if "-->" in s:
            continue
        # Skip numeric cue identifiers
        if s.isdigit():
            continue
        # Skip NOTE blocks
        if s.startswith("NOTE"):
            continue
        # Strip inline tags like <c>, <00:00:00.000>, <i>, etc.
        s = re.sub(r"<[^>]+>", "", s)
        # Strip cue settings like "align:start position:0%"
        s = re.sub(r"\s+align:\S+", "", s)
        s = re.sub(r"\s+position:\S+", "", s)
        s = html.unescape(s).strip()
        if s:
            text_lines.append(s)

    # Deduplicate consecutive repeats (common in YouTube auto-captions
    # where each cue repeats the tail of the previous one).
    deduped: list[str] = []
    for line in text_lines:
        if deduped and line == deduped[-1]:
            continue
        if deduped and line in deduped[-1]:
            continue
        if deduped and deduped[-1] in line:
            deduped[-1] = line
            continue
        deduped.append(line)

    # Join into paragraphs: wrap by sentence boundaries for readability.
    full = " ".join(deduped)
    full = re.sub(r"\s+", " ", full).strip()

    # Break into paragraph-ish chunks every ~4 sentences for readability.
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", full)
    paragraphs: list[str] = []
    buf: list[str] = []
    for sent in sentences:
        buf.append(sent)
        if len(buf) >= 4:
            paragraphs.append(" ".join(buf))
            buf = []
    if buf:
        paragraphs.append(" ".join(buf))

    return "\n\n".join(paragraphs)


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract clean YouTube transcript text.")
    ap.add_argument("url", help="YouTube video URL")
    ap.add_argument("--lang", default="en", help="Preferred subtitle language (default: en)")
    ap.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
        help=f"Root directory for organized output (default: {DEFAULT_ROOT})",
    )
    ap.add_argument("--stdout", action="store_true", help="Also print transcript to stdout")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        try:
            info, vtt = fetch_subtitles(args.url, args.lang, tmpdir)
        except Exception as e:
            print(f"Error fetching subtitles: {e}", file=sys.stderr)
            return 1

        if vtt is None:
            print("No subtitles (manual or auto) available for this video.", file=sys.stderr)
            return 2

        body = parse_vtt(vtt)

    title = info.get("title", "Unknown Title")
    channel = info.get("uploader", "Unknown")
    video_id = info.get("id", "unknown")
    duration = info.get("duration_string", info.get("duration", "n/a"))
    webpage_url = info.get("webpage_url", args.url)

    header = (
        f"# {title}\n"
        f"Channel : {channel}\n"
        f"Duration: {duration}\n"
        f"URL     : {webpage_url}\n"
        f"{'-' * 60}\n\n"
    )
    transcript_text = header + body + "\n"

    # Organize: <root>/<channel>/<title>_<videoid>/
    root = Path(args.root).expanduser()
    folder_name = f"{slugify(title)}_{video_id}"
    out_dir = root / slugify(channel) / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    transcript_path = out_dir / "transcript.txt"
    meta_path = out_dir / "metadata.json"

    transcript_path.write_text(transcript_text, encoding="utf-8")
    meta_path.write_text(
        json.dumps(
            {
                "id": video_id,
                "title": title,
                "channel": channel,
                "duration": duration,
                "url": webpage_url,
                "upload_date": info.get("upload_date"),
                "view_count": info.get("view_count"),
                "language": info.get("language") or args.lang,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Saved to: {out_dir}", file=sys.stderr)
    print(f"  - {transcript_path.name}", file=sys.stderr)
    print(f"  - {meta_path.name}", file=sys.stderr)

    if args.stdout:
        sys.stdout.write(transcript_text)

    return 0


if __name__ == "__main__":
    sys.exit(main())

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


def _build_ydl_opts(cookies_browser: str | None, extra: dict | None = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 10,
        "extractor_retries": 10,
        "fragment_retries": 10,
        "retry_sleep_functions": {
            "http": lambda n: min(2 ** n, 60),
            "fragment": lambda n: min(2 ** n, 60),
            "extractor": lambda n: min(2 ** n, 60),
        },
    }
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    if extra:
        opts.update(extra)
    return opts


def _run_with_cookie_fallback(opts: dict, url: str, *, download: bool) -> dict:
    """Run yt-dlp, retrying without browser cookies if the cookie jar is unreadable."""
    def _run(o: dict) -> dict:
        with YoutubeDL(o) as ydl:
            return ydl.extract_info(url, download=download)

    try:
        return _run(opts)
    except Exception as e:
        msg = str(e).lower()
        cookie_failure = any(k in msg for k in ("cookie", "secretstorage", "keyring", "browser"))
        if "cookiesfrombrowser" in opts and cookie_failure:
            opts.pop("cookiesfrombrowser", None)
            return _run(opts)
        raise


def probe_video(url: str, cookies_browser: str | None = None) -> dict:
    """Fetch video metadata (including available subtitle tracks) without downloading."""
    opts = _build_ydl_opts(cookies_browser, {"skip_download": True})
    return _run_with_cookie_fallback(opts, url, download=False)


def pick_subtitle_lang(info: dict, preferred: str | None) -> str:
    """Choose the best subtitle language.

    Priority:
      1. Explicit --lang from the user.
      2. The video's declared language (info.language) if subs exist for it.
      3. The first manual subtitle track available.
      4. The first automatic caption track available.
      5. Fallback to "en".
    """
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    def has_track(lang_code: str) -> bool:
        if not lang_code:
            return False
        prefix = lang_code.split("-")[0].lower()
        for key in list(manual.keys()) + list(auto.keys()):
            if key.lower() == prefix or key.lower().startswith(prefix + "-"):
                return True
        return False

    if preferred:
        return preferred

    declared = (info.get("language") or "").lower()
    if has_track(declared):
        return declared.split("-")[0]

    if manual:
        return next(iter(manual.keys())).split("-")[0]
    if auto:
        return next(iter(auto.keys())).split("-")[0]
    return "en"


def fetch_subtitles(
    url: str,
    lang: str,
    outdir: Path,
    cookies_browser: str | None = None,
) -> tuple[dict, Path | None]:
    """Download subtitles in the chosen language (manual preferred, else auto)."""
    # Restrict to the exact requested language family. We deliberately do NOT
    # fall back to other languages here: if French is requested but not present,
    # we'd rather report "missing" than silently grab an auto-translated track.
    opts = _build_ydl_opts(
        cookies_browser,
        {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [lang, f"{lang}.*", f"{lang}-orig"],
            "subtitlesformat": "vtt",
            "outtmpl": str(outdir / "%(id)s.%(ext)s"),
            "sleep_interval_subtitles": 1,
        },
    )

    info = _run_with_cookie_fallback(opts, url, download=True)

    video_id = info["id"]
    candidates = sorted(outdir.glob(f"{video_id}*.vtt"))
    if not candidates:
        return info, None

    # Prefer manual over auto: manual file is `<id>.<lang>.vtt`,
    # auto-generated is `<id>.<lang>.vtt` too but written second when both exist.
    # If only auto is present, yt-dlp writes it under the same name pattern.
    for c in candidates:
        if f".{lang}." in c.name or c.name.endswith(f".{lang}.vtt"):
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
    ap.add_argument(
        "--lang",
        default=None,
        help="Preferred subtitle language (e.g. fr, en). If omitted, auto-detect from the video.",
    )
    ap.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
        help=f"Root directory for organized output (default: {DEFAULT_ROOT})",
    )
    ap.add_argument("--stdout", action="store_true", help="Also print transcript to stdout")
    ap.add_argument(
        "--cookies-from-browser",
        default="chrome",
        help="Browser to load cookies from (chrome, firefox, brave, edge, ...) or 'none' to disable. Helps avoid 429s.",
    )
    args = ap.parse_args()

    cookies_browser = None if args.cookies_from_browser.lower() == "none" else args.cookies_from_browser

    try:
        probe = probe_video(args.url, cookies_browser)
    except Exception as e:
        print(f"Error probing video metadata: {e}", file=sys.stderr)
        return 1

    chosen_lang = pick_subtitle_lang(probe, args.lang)
    print(f"Subtitle language: {chosen_lang}", file=sys.stderr)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        try:
            info, vtt = fetch_subtitles(args.url, chosen_lang, tmpdir, cookies_browser)
        except Exception as e:
            print(f"Error fetching subtitles: {e}", file=sys.stderr)
            return 1

        if vtt is None:
            print(
                f"No '{chosen_lang}' subtitles (manual or auto) available for this video.",
                file=sys.stderr,
            )
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
                "language": info.get("language") or chosen_lang,
                "subtitle_language": chosen_lang,
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

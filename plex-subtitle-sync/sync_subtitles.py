#!/usr/bin/env python3
"""
Plex subtitle sync & fix.

For each configured TV show this tool will, per episode:
  1. Download a matching subtitle if one is missing (via subliminal).
  2. Re-time / fix the subtitle against the episode's actual audio (via ffsubsync),
     so the dialogue lines up even when the subtitle was made for a different release.
  3. Back up the original subtitle before overwriting it.
  4. Refresh the show in Plex so the corrected subtitle is picked up.

It is safe to run repeatedly: already-synced subtitles are skipped using a small
state file, so the systemd timer / cron job can run it on a schedule cheaply.

Configuration is via environment variables (see config.example.env):
  PLEX_BASEURL      e.g. http://192.168.0.46:32400
  PLEX_TOKEN        your X-Plex-Token
  PLEX_TV_SECTION   library section name (default: "TV Shows")
  SHOWS             comma-separated show titles
                    (default: "Matlock,High Potential,Saturday Night Live")
  SUB_LANGUAGE      ISO-639-1 language code (default: "en")
  OS_USERNAME       OpenSubtitles username (optional, improves download limits)
  OS_PASSWORD       OpenSubtitles password (optional)
  STATE_FILE        where to record processed files (default: ~/.plex_sub_sync.json)
  FORCE_RESYNC      "1" to re-sync everything, ignoring the state file
  DRY_RUN           "1" to log actions without modifying any files
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("plex-subtitle-sync")

SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".vtt")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else val


def load_config() -> dict:
    baseurl = env("PLEX_BASEURL")
    token = env("PLEX_TOKEN")
    if not baseurl or not token:
        LOG.error("PLEX_BASEURL and PLEX_TOKEN must be set. See config.example.env.")
        sys.exit(2)

    shows_raw = env("SHOWS", "Matlock,High Potential,Saturday Night Live (UK)")
    shows = [s.strip() for s in shows_raw.split(",") if s.strip()]

    return {
        "baseurl": baseurl,
        "token": token,
        "section": env("PLEX_TV_SECTION", "TV Shows"),
        "shows": shows,
        "language": env("SUB_LANGUAGE", "en"),
        "os_username": env("OS_USERNAME"),
        "os_password": env("OS_PASSWORD"),
        "state_file": Path(env("STATE_FILE", str(Path.home() / ".plex_sub_sync.json"))),
        "force": env("FORCE_RESYNC", "0") == "1",
        "dry_run": env("DRY_RUN", "0") == "1",
    }


# --------------------------------------------------------------------------- #
# State (so we don't re-sync the same file every run)
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def file_signature(video: Path) -> str:
    """Cheap signature: path + size + mtime. Detects file replacement/upgrade."""
    st = video.stat()
    raw = f"{video}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(raw.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Subtitle discovery / download / sync
# --------------------------------------------------------------------------- #
def find_subtitle(video: Path, language: str) -> Path | None:
    """Find an existing external subtitle next to the video for this language."""
    stem = video.stem
    candidates: list[Path] = []
    for f in video.parent.iterdir():
        if not f.is_file() or f.suffix.lower() not in SUBTITLE_EXTS:
            continue
        if not f.name.startswith(stem):
            continue
        candidates.append(f)

    if not candidates:
        return None

    # Prefer one tagged with the requested language (e.g. movie.en.srt).
    for f in candidates:
        mid = f.name[len(stem):].lower()
        if f".{language}." in mid or mid.startswith(f".{language}"):
            return f
    return candidates[0]


def download_subtitle(video: Path, language: str, cfg: dict) -> Path | None:
    """Download a best-match subtitle using subliminal."""
    try:
        from babelfish import Language
        from subliminal import download_best_subtitles, region, save_subtitles, scan_video
    except ImportError:
        LOG.warning("subliminal not installed; cannot download missing subtitle for %s", video.name)
        return None

    region.configure("dogpile.cache.memory", expiration_time=3600)

    provider_configs = {}
    if cfg["os_username"] and cfg["os_password"]:
        provider_configs["opensubtitlescom"] = {
            "username": cfg["os_username"],
            "password": cfg["os_password"],
        }

    try:
        scanned = scan_video(str(video))
    except ValueError as exc:
        LOG.warning("Could not parse %s for subtitle search: %s", video.name, exc)
        return None

    lang = {Language.fromietf(language)}
    if cfg["dry_run"]:
        LOG.info("[dry-run] would download %s subtitle for %s", language, video.name)
        return None

    found = download_best_subtitles({scanned}, lang, provider_configs=provider_configs or None)
    subs = found.get(scanned, [])
    if not subs:
        LOG.info("No downloadable subtitle found for %s", video.name)
        return None

    save_subtitles(scanned, subs)
    sub = find_subtitle(video, language)
    if sub:
        LOG.info("Downloaded subtitle: %s", sub.name)
    return sub


def sync_subtitle(video: Path, subtitle: Path, cfg: dict) -> bool:
    """Re-time the subtitle against the video's audio using ffsubsync.

    Returns True if a synced subtitle was written.
    """
    ffs = shutil.which("ffsubsync") or shutil.which("ffs")
    if not ffs:
        LOG.error("ffsubsync not found on PATH; cannot fix subtitle timing.")
        return False

    synced_tmp = subtitle.with_suffix(subtitle.suffix + ".synced")
    cmd = [ffs, str(video), "-i", str(subtitle), "-o", str(synced_tmp)]

    if cfg["dry_run"]:
        LOG.info("[dry-run] would run: %s", " ".join(cmd))
        return False

    LOG.info("Syncing %s", subtitle.name)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not synced_tmp.exists():
        LOG.error("ffsubsync failed for %s: %s", subtitle.name, result.stderr.strip()[-500:])
        synced_tmp.unlink(missing_ok=True)
        return False

    # Back up the original once, then replace it with the synced version.
    backup = subtitle.with_suffix(subtitle.suffix + ".orig")
    if not backup.exists():
        shutil.copy2(subtitle, backup)
    synced_tmp.replace(subtitle)
    LOG.info("Replaced %s with synced subtitle (backup: %s)", subtitle.name, backup.name)
    return True


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def iter_episode_files(show) -> list[Path]:
    paths: list[Path] = []
    for episode in show.episodes():
        for media in episode.media:
            for part in media.parts:
                if part.file:
                    paths.append(Path(part.file))
    return paths


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cfg = load_config()

    try:
        from plexapi.server import PlexServer
    except ImportError:
        LOG.error("plexapi not installed. Run: pip install -r requirements.txt")
        return 2

    LOG.info("Connecting to Plex at %s", cfg["baseurl"])
    plex = PlexServer(cfg["baseurl"], cfg["token"])

    try:
        section = plex.library.section(cfg["section"])
    except Exception as exc:  # plexapi raises NotFound
        LOG.error("Library section %r not found: %s", cfg["section"], exc)
        return 2

    state = load_state(cfg["state_file"])
    processed = changed = skipped = missing = 0
    touched_shows = []

    for title in cfg["shows"]:
        try:
            show = section.get(title)
        except Exception:
            LOG.warning("Show %r not found in section %r; skipping.", title, cfg["section"])
            continue

        LOG.info("== %s ==", show.title)
        for video in iter_episode_files(show):
            if not video.exists():
                LOG.warning("Episode file not accessible from this host: %s", video)
                missing += 1
                continue

            sig = file_signature(video)
            if not cfg["force"] and state.get(str(video)) == sig:
                skipped += 1
                continue

            subtitle = find_subtitle(video, cfg["language"])
            if subtitle is None:
                subtitle = download_subtitle(video, cfg["language"], cfg)
            if subtitle is None:
                missing += 1
                continue

            processed += 1
            if sync_subtitle(video, subtitle, cfg):
                changed += 1
                touched_shows.append(show)
                if not cfg["dry_run"]:
                    state[str(video)] = sig

        if not cfg["dry_run"]:
            save_state(cfg["state_file"], state)

    # Refresh shows whose subtitles changed so Plex re-scans them.
    for show in {s.ratingKey: s for s in touched_shows}.values():
        if not cfg["dry_run"]:
            LOG.info("Refreshing Plex metadata for %s", show.title)
            show.refresh()

    LOG.info(
        "Done. processed=%d synced=%d skipped=%d missing/unfound=%d",
        processed, changed, skipped, missing,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

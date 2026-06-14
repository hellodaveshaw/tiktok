# Plex subtitle sync & fix

Automatically downloads missing subtitles and **fixes subtitle timing against the
actual episode audio** for selected Plex shows, then refreshes Plex so the
corrected subtitles show up. Designed to run on a schedule and forget about it.

By default it targets three shows: **Matlock**, **High Potential**, and
**Saturday Night Live** — change the `SHOWS` list in `config.env` for others.

## How it works

For every episode of each configured show it will:

1. Look for an external subtitle next to the video file.
2. If none exists, download a best match with [subliminal](https://github.com/Diaoul/subliminal).
3. Re-time the subtitle against the audio with [ffsubsync](https://github.com/smacke/ffsubsync)
   (this is the part that fixes out-of-sync subs from a different release).
4. Back up the original (`.srt.orig`) and replace it with the synced version.
5. Refresh the show in Plex.

A small state file means re-runs only process new/changed episodes, so the
scheduled job is cheap.

> **Run it on the NAS / Plex host.** It needs LAN access to Plex *and* direct
> filesystem access to the media (the same paths Plex sees). It cannot run from
> a cloud sandbox that has no route to your local network.

## Install (one time, on the NAS)

```bash
git clone https://github.com/hellodaveshaw/tiktok.git
cd tiktok/plex-subtitle-sync
sudo ./install.sh                 # or: PREFIX=~/plex-subtitle-sync ./install.sh
nano /opt/plex-subtitle-sync/config.env   # set PLEX_TOKEN (and shows/language)
```

`install.sh` creates a virtualenv, installs the Python deps, and — if `systemd`
is present — installs a timer that runs every 6 hours. Without systemd it prints
the cron / Synology Task Scheduler line to use. You also need **ffmpeg**
installed (ffsubsync depends on it).

## Run manually

```bash
cd /opt/plex-subtitle-sync
set -a && . ./config.env && set +a
./.venv/bin/python ./sync_subtitles.py
```

Useful toggles in `config.env`:

- `DRY_RUN=1` — log what it *would* do without touching files (try this first).
- `FORCE_RESYNC=1` — re-sync everything, ignoring the state file.
- `SUB_LANGUAGE=en` — change subtitle language.

## Notes & alternatives

- Originals are always backed up to `*.orig` before replacement, and ffsubsync
  is non-destructive on failure.
- For a full-featured GUI that does the same job continuously (and integrates
  with Sonarr), consider **Bazarr** with its ffsubsync post-processing enabled —
  this tool is the lightweight, scriptable equivalent for just these shows.

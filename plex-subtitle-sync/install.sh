#!/usr/bin/env bash
#
# One-time installer for the Plex subtitle sync tool.
# Run this ON the NAS / machine that runs Plex (it needs LAN access to Plex
# and direct access to the media files).
#
#   sudo ./install.sh            # installs to /opt/plex-subtitle-sync + systemd timer
#   PREFIX=~/plex-subtitle-sync ./install.sh   # user install, no sudo
#
set -euo pipefail

PREFIX="${PREFIX:-/opt/plex-subtitle-sync}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ">> Installing to $PREFIX"
mkdir -p "$PREFIX"
cp -f "$SRC_DIR/sync_subtitles.py" "$PREFIX/"
cp -f "$SRC_DIR/requirements.txt" "$PREFIX/"
[ -f "$PREFIX/config.env" ] || cp -n "$SRC_DIR/config.example.env" "$PREFIX/config.env"

echo ">> Checking ffmpeg (required by ffsubsync)"
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "!! ffmpeg not found. Install it first:"
  echo "     Debian/Ubuntu: sudo apt install ffmpeg"
  echo "     Synology:      install 'ffmpeg' via Package Center / Entware"
  echo "     macOS:         brew install ffmpeg"
  echo "Aborting: ffmpeg is required. Re-run install.sh once it is installed."
  exit 1
fi

echo ">> Creating virtualenv and installing Python deps"
python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install --upgrade pip >/dev/null
"$PREFIX/.venv/bin/pip" install -r "$PREFIX/requirements.txt"

echo ">> Edit your config now:  $PREFIX/config.env  (set PLEX_TOKEN at minimum)"

if command -v systemctl >/dev/null 2>&1 && [ "${EUID:-$(id -u)}" -eq 0 ]; then
  echo ">> Installing systemd timer (runs every 6h)"
  sed "s#/opt/plex-subtitle-sync#$PREFIX#g" "$SRC_DIR/systemd/plex-subtitle-sync.service" \
    > /etc/systemd/system/plex-subtitle-sync.service
  cp -f "$SRC_DIR/systemd/plex-subtitle-sync.timer" /etc/systemd/system/plex-subtitle-sync.timer
  systemctl daemon-reload
  systemctl enable --now plex-subtitle-sync.timer
  echo ">> Done. Run once now with:  systemctl start plex-subtitle-sync.service"
  echo "   View logs with:           journalctl -u plex-subtitle-sync.service -f"
else
  CRON_CMD="cd $PREFIX && set -a && . ./config.env && set +a && ./.venv/bin/python ./sync_subtitles.py >> $PREFIX/sync.log 2>&1"
  echo ">> No systemd (or not root). Add this cron entry to run every 6 hours:"
  echo "   crontab -e   then add:"
  echo "   0 */6 * * * $CRON_CMD"
  echo "   (On Synology DSM, use Control Panel > Task Scheduler instead, running the same command.)"
fi

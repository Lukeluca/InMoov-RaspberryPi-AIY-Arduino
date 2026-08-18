#!/bin/bash
# Installs Gary's systemd units so he starts on power-on.
#
# These are USER units, not system units, and that is deliberate. The AIY
# voiceHAT is held exclusively by PulseAudio, which runs inside the desktop
# user's systemd session. A system-scope service cannot reach it — aplay fails
# with "Device or resource busy" — so text-to-speech is silent while everything
# else appears to work. Running in the user session fixes that.
#
# Lingering is enabled so the user session starts at power-on without anyone
# logging in. That step needs sudo; nothing else does.
#
#   ./scripts/install-systemd.sh
#
# Afterwards:
#   systemctl --user start gary.target      start everything now
#   systemctl --user stop gary.target       stop everything
#   systemctl --user status gary-servo      check one service
#   journalctl --user -u gary-brain -f      follow a service's log

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(id -un)"
UNIT_SRC="$ROOT/scripts/systemd"
DEST="$HOME/.config/systemd/user"

echo "Installing from : $ROOT"
echo "For user        : $RUN_USER"

mkdir -p "$DEST"

for unit in "$UNIT_SRC"/*.service "$UNIT_SRC"/*.target; do
    name="$(basename "$unit")"
    sed -e "s|/home/pi/gary|$ROOT|g" "$unit" > "$DEST/$name"
    echo "  installed $name"
done

systemctl --user daemon-reload
systemctl --user enable gary.target

# Without lingering the user session only exists while someone is logged in,
# so Gary would not start on a headless power-on.
sudo loginctl enable-linger "$RUN_USER"
echo "  lingering enabled for $RUN_USER"

echo
echo "Enabled. Gary will start on power-on."
echo "Start him now with: systemctl --user start gary.target"

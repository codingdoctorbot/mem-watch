#!/bin/sh
# Install: autostart on login + entry in the app menu. Uninstall: ./install.sh --remove
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
AUTOSTART="$HOME/.config/autostart/mem-watch.desktop"
MENU="$HOME/.local/share/applications/mem-watch.desktop"

if [ "$1" = "--remove" ]; then
    rm -f "$AUTOSTART" "$MENU"
    pkill -f "$DIR/mem_watch.py" || true
    echo "Removed."; exit 0
fi

[ -x "$DIR/.venv/bin/python" ] || { python3 -m venv "$DIR/.venv" && "$DIR/.venv/bin/pip" install -q PyQt6; }
mkdir -p "$(dirname "$AUTOSTART")" "$(dirname "$MENU")"
cat > "$AUTOSTART" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Memory Watch
Comment=Tray monitor for RAM, swap, GPU-driver memory leaks and crashes
Exec="$DIR/.venv/bin/python" "$DIR/mem_watch.py"
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;
X-KDE-autostart-after=panel
DESKTOP
cp "$AUTOSTART" "$MENU"
echo "Installed. Starts on login; run now with: \"$DIR/.venv/bin/python\" \"$DIR/mem_watch.py\" &"

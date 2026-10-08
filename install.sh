#!/bin/sh
# Install / reload the launchd agent (runs every 5 minutes).
set -eu
cd "$(dirname "$0")"
LABEL=com.scayle.todoist-sync
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

security find-generic-password -s todoist-sync -w >/dev/null 2>&1 || {
  echo "No token in Keychain. Run first:"
  echo '  security add-generic-password -s todoist-sync -a "$USER" -w <token>'
  exit 1
}

mkdir -p "$HOME/Library/LaunchAgents"
cp "$LABEL.plist" "$DEST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "Installed. Log: ~/Library/Logs/todoist-sync.log"

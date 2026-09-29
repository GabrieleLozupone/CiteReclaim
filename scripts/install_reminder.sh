#!/usr/bin/env bash
# Installs (or removes) a monthly citereclaim reminder on macOS.
# Creates a LaunchAgent that runs scripts/remind.sh on day 1 of every month at 10:00.
#
# Usage:
#   ./scripts/install_reminder.sh              # install / update
#   ./scripts/install_reminder.sh --uninstall  # remove
#   ./scripts/install_reminder.sh --test       # show the reminder now
#
# Optional variables:
#   DAY (default 1), HOUR (default 10), MINUTE (default 0)
#   BROWSER_APP   app for Google Scholar (default: the system browser)
#   TERMINAL_APP  terminal app (default: Terminal)
#   e.g. DAY=15 HOUR=9 BROWSER_APP="Google Chrome" TERMINAL_APP=iTerm ./scripts/install_reminder.sh
set -euo pipefail

LABEL="${LABEL:-io.github.citereclaim.reminder}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SCRIPT="$(cd "$(dirname "$0")" && pwd)/remind.sh"
DAY="${DAY:-1}"
HOUR="${HOUR:-10}"
MINUTE="${MINUTE:-0}"
BROWSER_APP="${BROWSER_APP:-}"
TERMINAL_APP="${TERMINAL_APP:-Terminal}"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "This reminder uses launchd and only works on macOS." >&2
    exit 1
fi

case "${1:-}" in
    --uninstall)
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
        rm -f "$PLIST"
        echo "Reminder removed."
        exit 0
        ;;
    --test)
        launchctl kickstart "$DOMAIN/$LABEL"
        exit 0
        ;;
esac

chmod +x "$SCRIPT"
mkdir -p "$(dirname "$PLIST")"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SCRIPT</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>BROWSER_APP</key>
        <string>$BROWSER_APP</string>
        <key>TERMINAL_APP</key>
        <string>$TERMINAL_APP</string>
    </dict>
    <!-- Day $DAY of every month at $HOUR:$(printf %02d "$MINUTE") -->
    <key>StartCalendarInterval</key>
    <dict><key>Day</key><integer>$DAY</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
    <key>StandardErrorPath</key>
    <string>/tmp/citereclaim-reminder.log</string>
</dict>
</plist>
PLIST

plutil -lint -s "$PLIST"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"

printf 'Reminder installed: day %s of every month at %s:%02d\n' "$DAY" "$HOUR" "$MINUTE"
echo "Script: $SCRIPT"
echo "Test it: $0 --test"

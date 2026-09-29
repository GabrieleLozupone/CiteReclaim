#!/usr/bin/env bash
# Installa (o rimuove) il promemoria mensile di citation-reconciler su macOS.
# Crea un LaunchAgent che il 1° di ogni mese alle 10:00 esegue scripts/remind.sh.
#
# Uso:
#   ./scripts/install_reminder.sh              # installa / aggiorna
#   ./scripts/install_reminder.sh --uninstall  # rimuove
#   ./scripts/install_reminder.sh --test       # mostra subito il promemoria
#
# Variabili opzionali: DAY (default 1), HOUR (default 10), MINUTE (default 0)
#   es. DAY=15 HOUR=9 ./scripts/install_reminder.sh
set -euo pipefail

LABEL="it.unicas.citation-reconciler.reminder"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SCRIPT="$(cd "$(dirname "$0")" && pwd)/remind.sh"
DAY="${DAY:-1}"
HOUR="${HOUR:-10}"
MINUTE="${MINUTE:-0}"

case "${1:-}" in
    --uninstall)
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
        rm -f "$PLIST"
        echo "Promemoria rimosso."
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
    <!-- Il giorno $DAY di ogni mese alle $HOUR:$(printf %02d "$MINUTE") -->
    <key>StartCalendarInterval</key>
    <dict><key>Day</key><integer>$DAY</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
    <key>StandardErrorPath</key>
    <string>/tmp/citation-reconciler-reminder.log</string>
</dict>
</plist>
PLIST

plutil -lint -s "$PLIST"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"

printf 'Promemoria installato: giorno %s di ogni mese alle %s:%02d\n' "$DAY" "$HOUR" "$MINUTE"
echo "Script: $SCRIPT"
echo "Prova: $0 --test"

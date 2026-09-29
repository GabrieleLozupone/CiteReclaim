#!/usr/bin/env bash
# Periodic reminder (started by launchd, see install_reminder.sh): shows the steps to follow
# and, on request, opens Google Scholar, the exports folder and a terminal in the project.
#
# Optional variables (set by install_reminder.sh):
#   BROWSER_APP   app used for Google Scholar (default: the system browser), e.g. "Google Chrome"
#   TERMINAL_APP  terminal app (default: Terminal), e.g. iTerm
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TERMINAL_APP="${TERMINAL_APP:-Terminal}"
SCHOLAR_URL="https://scholar.google.com/citations"

MSG="Time to check for citations missing from Scopus.

1. In Google Scholar, open 'Cited by' for each paper, save the citing articles to My library, select them → Export → BibTeX.
2. Replace the files in scholar_exports/ (one per paper, e.g. <NAME>.bib).
3. Connect to your institution's network/VPN (needed for the Scopus API).
4. In the terminal run:  ./run.sh
5. Send Scopus support output/<NAME>/scopus_support_request.txt + scopus_reference_linking.xlsx."

BUTTON=$(osascript -e "button returned of (display dialog \"$MSG\" with title \"CiteReclaim\" buttons {\"Later\", \"Start\"} default button \"Start\")" 2>/dev/null || echo "Later")

if [[ "$BUTTON" == "Start" ]]; then
    if [[ -n "${BROWSER_APP:-}" ]]; then
        open -a "$BROWSER_APP" "$SCHOLAR_URL"
    else
        open "$SCHOLAR_URL"
    fi
    mkdir -p "$PROJECT_DIR/scholar_exports"
    open "$PROJECT_DIR/scholar_exports"
    open -a "$TERMINAL_APP" "$PROJECT_DIR"
fi

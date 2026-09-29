#!/usr/bin/env bash
# Promemoria periodico (lanciato da launchd): mostra i passi da fare e, se richiesto,
# apre Google Scholar, la cartella del progetto e un Terminale già posizionato lì.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

MSG="È ora di aggiornare le segnalazioni Scopus.

1. Da Google Scholar: 'Citato da' di ogni articolo → seleziona tutto → esporta BibTeX.
2. Sostituisci i file in scholar_exports/ (es. LDAE.bib, AXIAL.bib).
3. Connettiti alla rete/VPN dell'università (per la API Scopus).
4. Nel Terminale esegui:  ./run.sh
5. Invia a Scopus output/<ARTICOLO>/scopus_support_request.txt + scopus_reference_linking.xlsx."

BUTTON=$(osascript -e "button returned of (display dialog \"$MSG\" with title \"Citation Reconciler\" buttons {\"Più tardi\", \"Inizia\"} default button \"Inizia\")" 2>/dev/null || echo "Più tardi")

if [[ "$BUTTON" == "Inizia" ]]; then
    open -a "Google Chrome" "https://scholar.google.com/citations"
    open "$PROJECT_DIR/scholar_exports"
    open -a iTerm "$PROJECT_DIR"
fi

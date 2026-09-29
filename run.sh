#!/usr/bin/env bash
# Esegue l'intero flusso di citation-reconciler:
#   init → aggiornamento Scopus Source List → import articoli → import Google Scholar
#   (scholar_exports/<NOME>.bib|.csv|.json, se presenti) → sync → report → export
#
# Uso:
#   ./run.sh                      # usa examples/papers.yaml
#   ./run.sh miei_articoli.yaml   # usa un altro file YAML di articoli
#
# Variabili opzionali:
#   REFRESH=1 ./run.sh            # ignora la cache HTTP e riscarica tutto
#   DETAILS=1 ./run.sh            # report con tutte le evidenze
set -euo pipefail

cd "$(dirname "$0")"
PAPERS_FILE="${1:-examples/papers.yaml}"

# --- ambiente Python -------------------------------------------------------
if [[ ! -x .venv/bin/citation-reconciler ]]; then
    echo "==> Creo il virtualenv e installo il pacchetto"
    if command -v uv >/dev/null 2>&1; then
        uv venv -q -p 3.12 .venv
        uv pip install -q --python .venv/bin/python -e .
    else
        python3.12 -m venv .venv
        .venv/bin/pip install -q -e .
    fi
fi
CR=.venv/bin/citation-reconciler

if [[ ! -f "$PAPERS_FILE" ]]; then
    echo "File articoli non trovato: $PAPERS_FILE" >&2
    exit 1
fi

SYNC_FLAGS=()
[[ "${REFRESH:-0}" == "1" ]] && SYNC_FLAGS+=(--refresh)
REPORT_FLAGS=()
[[ "${DETAILS:-0}" == "1" ]] && REPORT_FLAGS+=(--details)

# --- flusso ----------------------------------------------------------------
echo "==> Inizializzazione"
"$CR" init >/dev/null

echo "==> Scopus Source List (scaricata solo se più vecchia di 30 giorni)"
if ! "$CR" scopus-sources update; then
    echo "!! Aggiornamento automatico fallito: continuo con la lista locale (se presente)." >&2
fi

echo "==> Import articoli da $PAPERS_FILE"
"$CR" paper import "$PAPERS_FILE"

# Export di Google Scholar: scholar_exports/<NOME>.bib|.csv|.json (NOME = nome dell'articolo).
# Le righe già importate vengono ignorate, quindi rilanciare è sicuro.
SCHOLAR_DIR="${SCHOLAR_DIR:-scholar_exports}"
PAPER_NAMES=$("$CR" paper list --json | .venv/bin/python -c \
    "import json, sys; print('\n'.join(p['name'] for p in json.load(sys.stdin)))")
while IFS= read -r name; do
    [[ -z "$name" ]] && continue
    for ext in bib csv json; do
        f="$SCHOLAR_DIR/$name.$ext"
        if [[ -f "$f" ]]; then
            echo "==> Import Google Scholar per $name da $f"
            "$CR" scholar import "$f" --paper "$name" --no-sync
        fi
    done
done <<< "$PAPER_NAMES"

echo "==> Sync citazioni (può richiedere qualche minuto)"
# Un sync parziale (es. rate limit di Semantic Scholar) non deve fermare lo script.
"$CR" sync --all "${SYNC_FLAGS[@]+"${SYNC_FLAGS[@]}"}" || echo "!! Alcuni sync hanno avuto errori; vedi sopra." >&2

echo "==> Report"
"$CR" report --all "${REPORT_FLAGS[@]+"${REPORT_FLAGS[@]}"}"

echo "==> Export"
mkdir -p output
"$CR" export --format csv --output output/report.csv
"$CR" export --format json --output output/report.json
while IFS= read -r name; do
    [[ -n "$name" ]] && "$CR" export-support "$name"
done <<< "$PAPER_NAMES"

echo
echo "Fatto. File generati in: $(pwd)/output/"

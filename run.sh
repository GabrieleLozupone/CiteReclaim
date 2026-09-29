#!/usr/bin/env bash
# Runs the whole CiteReclaim workflow:
#   init → Scopus Source List update → paper import → Google Scholar import
#   (scholar_exports/<NAME>.bib|.csv|.json, if present) → sync → report → export
#
# Usage:
#   ./run.sh                      # uses examples/papers.yaml
#   ./run.sh my_papers.yaml       # uses another YAML file of papers
#
# Optional variables:
#   REFRESH=1 ./run.sh            # bypass the HTTP cache and download everything again
#   DETAILS=1 ./run.sh            # report with all the evidence
set -euo pipefail

cd "$(dirname "$0")"
PAPERS_FILE="${1:-examples/papers.yaml}"

# --- Python environment -------------------------------------------------------
if [[ ! -x .venv/bin/citereclaim ]]; then
    echo "==> Creating the virtualenv and installing the package"
    if command -v uv >/dev/null 2>&1; then
        uv venv -q -p 3.12 .venv
        uv pip install -q --python .venv/bin/python -e .
    else
        python3.12 -m venv .venv
        .venv/bin/pip install -q -e .
    fi
fi
CR=.venv/bin/citereclaim

if [[ ! -f "$PAPERS_FILE" ]]; then
    echo "Papers file not found: $PAPERS_FILE" >&2
    exit 1
fi

SYNC_FLAGS=()
[[ "${REFRESH:-0}" == "1" ]] && SYNC_FLAGS+=(--refresh)
REPORT_FLAGS=()
[[ "${DETAILS:-0}" == "1" ]] && REPORT_FLAGS+=(--details)

# --- workflow ----------------------------------------------------------------
echo "==> Initialising"
"$CR" init >/dev/null

echo "==> Scopus Source List (downloaded only if older than 30 days)"
if ! "$CR" scopus-sources update; then
    echo "!! Automatic update failed: continuing with the local list (if any)." >&2
fi

echo "==> Importing papers from $PAPERS_FILE"
"$CR" paper import "$PAPERS_FILE"

# Google Scholar exports: scholar_exports/<NAME>.bib|.csv|.json (NAME = the paper's name).
# Rows already imported are skipped, so re-running is safe.
SCHOLAR_DIR="${SCHOLAR_DIR:-scholar_exports}"
PAPER_NAMES=$("$CR" paper list --json | .venv/bin/python -c \
    "import json, sys; print('\n'.join(p['name'] for p in json.load(sys.stdin)))")
while IFS= read -r name; do
    [[ -z "$name" ]] && continue
    for ext in bib csv json; do
        f="$SCHOLAR_DIR/$name.$ext"
        if [[ -f "$f" ]]; then
            echo "==> Importing Google Scholar results for $name from $f"
            "$CR" scholar import "$f" --paper "$name" --no-sync
        fi
    done
done <<< "$PAPER_NAMES"

echo "==> Syncing citations (may take a few minutes)"
# A partial sync (e.g. a Semantic Scholar rate limit) must not stop the script.
"$CR" sync --all "${SYNC_FLAGS[@]+"${SYNC_FLAGS[@]}"}" || echo "!! Some syncs reported errors; see above." >&2

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
echo "Done. Files written to: $(pwd)/output/"

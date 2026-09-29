"""Import manually exported Google Scholar "Cited by" lists (CSV, BibTeX, JSON).

Google Scholar is never queried or scraped. Users export/copy results themselves
(e.g. via Publish or Perish, a reference manager, or by hand) and import the file.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..matching import find_arxiv_ids, find_dois, normalize_doi

# Canonical field -> accepted column names (compared case/space-insensitively).
ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "article title", "paper title", "document title", "name"),
    "authors": ("authors", "author", "author(s)", "creators", "author names"),
    "year": ("year", "publication year", "pub year", "date", "published", "issued"),
    "journal": (
        "journal",
        "venue",
        "source",
        "publication",
        "journal title",
        "source title",
        "container title",
        "booktitle",
        "conference",
        "publisher/venue",
    ),
    "doi": ("doi", "doi link"),
    "url": ("url", "link", "articleurl", "article url", "full text url", "fulltexturl"),
    "publisher": ("publisher",),
    "type": ("type", "document type", "item type"),
    "cites": ("cites", "citations", "cited by", "citation count"),
}


def _key(s: str) -> str:
    return re.sub(r"[\s_\-]+", " ", s.strip().lower())


def _canonical_row(raw: dict[str, Any]) -> dict[str, Any]:
    lookup = {_key(k): v for k, v in raw.items() if k is not None}
    row: dict[str, Any] = {}
    for field, names in ALIASES.items():
        for n in names:
            v = lookup.get(_key(n))
            if v not in (None, ""):
                row[field] = v.strip() if isinstance(v, str) else v
                break
    if isinstance(row.get("authors"), list):
        row["authors"] = "; ".join(str(a) for a in row["authors"])
    year = row.get("year")
    if year is not None:
        m = re.search(r"(19|20)\d{2}", str(year))
        row["year"] = int(m.group(0)) if m else None
    doi = normalize_doi(str(row["doi"])) if row.get("doi") else None
    if not doi:
        for candidate in (row.get("url"), row.get("journal")):
            found = find_dois(str(candidate)) if candidate else []
            if found:
                doi = found[0]
                break
    row["doi"] = doi
    arxiv = find_arxiv_ids(" ".join(str(row.get(k) or "") for k in ("url", "journal", "doi")))
    row["arxiv"] = arxiv[0] if arxiv else None
    return row


def split_authors(value: str | None) -> list[str]:
    if not value:
        return []
    value = value.replace("…", "").replace("...", "")
    if " and " in value:
        parts = value.split(" and ")
    elif ";" in value:
        parts = value.split(";")
    else:
        parts = value.split(",")
    return [p.strip() for p in parts if p.strip()]


def row_hash(row: dict[str, Any]) -> str:
    basis = json.dumps(
        {k: row.get(k) for k in ("title", "doi", "year", "journal")}, sort_keys=True, default=str
    )
    return hashlib.sha256(basis.encode()).hexdigest()[:24]


# --------------------------------------------------------------------------- formats


def parse_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return [_canonical_row(r) for r in csv.DictReader(fh, dialect=dialect)]


def parse_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("citations", "items", "results", "data", "papers"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError("JSON must be a list of objects or contain a 'citations' list")
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        # CSL-JSON support: title/author/issued/container-title/DOI/URL.
        flat = dict(item)
        if isinstance(item.get("author"), list):
            flat["authors"] = [
                " ".join(x for x in (a.get("given"), a.get("family")) if x)
                if isinstance(a, dict)
                else str(a)
                for a in item["author"]
            ]
            flat.pop("author", None)
        if isinstance(item.get("issued"), dict):
            parts = item["issued"].get("date-parts") or [[None]]
            flat["year"] = parts[0][0]
            flat.pop("issued", None)
        if item.get("container-title"):
            ct = item["container-title"]
            flat["journal"] = ct[0] if isinstance(ct, list) and ct else ct
        rows.append(_canonical_row(flat))
    return rows


_LATEX_ACCENTS = {
    "'": "\u0301",
    "`": "\u0300",
    "^": "\u0302",
    '"': "\u0308",
    "~": "\u0303",
    "c": "\u0327",
    "=": "\u0304",
    ".": "\u0307",
    "u": "\u0306",
    "v": "\u030c",
}
_LATEX_ACCENT_RE = re.compile(r"\\(['`^\"~=.]|[cuv](?=[\s{]))\s*\{?([A-Za-z])\}?")
_LATEX_SPECIAL_RE = re.compile(r"\\([&%$#_])")


def latex_to_unicode(text: str) -> str:
    """Decode the common BibTeX escapes ({\\'o} -> ó, \\& -> &)."""
    import unicodedata

    text = re.sub(r"\\i(?![A-Za-z])", "i", text)  # dotless i, as in {\\'\\i}
    text = _LATEX_ACCENT_RE.sub(
        lambda m: unicodedata.normalize("NFC", m.group(2) + _LATEX_ACCENTS[m.group(1)]), text
    )
    return _LATEX_SPECIAL_RE.sub(r"\1", text)


_BIB_ENTRY_RE = re.compile(r"@(\w+)\s*\{\s*([^,\s]*)\s*,", re.MULTILINE)


def _bib_value(text: str, i: int) -> tuple[str, int]:
    """Read a braced, quoted or bare BibTeX value starting at ``text[i]``."""
    while i < len(text) and text[i].isspace():
        i += 1
    if i < len(text) and text[i] == "{":
        depth, j = 0, i
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    return text[i + 1 : j], j + 1
            j += 1
        return text[i + 1 :], len(text)
    if i < len(text) and text[i] == '"':
        j = i + 1
        while j < len(text) and not (text[j] == '"' and text[j - 1] != "\\"):
            j += 1
        return text[i + 1 : j], j + 1
    m = re.match(r"[^,}\s]+", text[i:])
    return (m.group(0), i + m.end()) if m else ("", i)


def parse_bibtex_text(text: str) -> list[dict[str, Any]]:
    entries = []
    for m in _BIB_ENTRY_RE.finditer(text):
        etype = m.group(1).lower()
        if etype in ("comment", "preamble", "string"):
            continue
        i = m.end()
        fields: dict[str, str] = {"type": etype}
        while i < len(text):
            fm = re.compile(r"\s*([A-Za-z][\w\-]*)\s*=").match(text, i)
            if not fm:
                break
            value, i = _bib_value(text, fm.end())
            value = latex_to_unicode(value)
            fields[fm.group(1).lower()] = re.sub(
                r"\s+", " ", value.replace("{", "").replace("}", "")
            ).strip()
            while i < len(text) and text[i] in " \t\r\n,":
                i += 1
            if i < len(text) and text[i] == "}":
                break
        if "journal" not in fields:
            for alt in ("booktitle", "school", "publisher", "howpublished"):
                if fields.get(alt):
                    fields["journal"] = fields[alt]
                    break
        fields["authors"] = fields.pop("author", "")
        entries.append(_canonical_row(fields))
    return entries


def parse_bibtex(path: Path) -> list[dict[str, Any]]:
    return parse_bibtex_text(path.read_text(encoding="utf-8"))


def parse_file(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv", ".txt"):
        rows = parse_csv(path)
    elif suffix in (".bib", ".bibtex"):
        rows = parse_bibtex(path)
    elif suffix == ".json":
        rows = parse_json(path)
    else:
        raise ValueError(f"unsupported Scholar export format {suffix!r} (use .csv, .bib, .json)")
    return [r for r in rows if r.get("title") or r.get("doi")]

"""Scopus Source Title List: discovery, download, parsing, local index and matching.

The list is published monthly by Elsevier as an .xlsx file linked from
https://www.elsevier.com/products/scopus/content. The download URL changes with
every release, so it is discovered from that page instead of being hard-coded.
A manual ``import`` of a locally downloaded .xlsx/.csv is always possible.
"""

from __future__ import annotations

import csv
import hashlib
import html
import re
import urllib.robotparser
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..db import Database, now_iso
from ..matching import normalize_isbn, normalize_issn, normalize_title
from ..models import ScopusSourceStatus, SourceMatch, WorkRecord

CONTENT_PAGE = "https://www.elsevier.com/products/scopus/content"

# --------------------------------------------------------------------------- discovery


def discover_download_url(page_html: str, base: str = CONTENT_PAGE) -> tuple[str, str | None]:
    """Find the Source Title List asset link in the content page.

    Returns ``(url, label)``. Raises ``LookupError`` if nothing plausible is found.
    """
    text = html.unescape(page_html)
    candidates: list[tuple[int, str, str | None]] = []
    # 1) Structured Contentful JSON embedded in the page: {"title": "...source title list...",
    #    "url": "//downloads.ctfassets.net/...xlsx"}
    for m in re.finditer(
        r'"title"\s*:\s*"([^"]*source title list[^"]*)"\s*,\s*"url"\s*:\s*"([^"]+\.xlsx)"',
        text,
        flags=re.IGNORECASE,
    ):
        candidates.append((3, m.group(2), m.group(1)))
    # 2) Anchor tags whose label mentions the source title list.
    for m in re.finditer(r"<a\b[^>]*href=\"([^\"]+\.xlsx)\"[^>]*>", text, flags=re.IGNORECASE):
        tag = m.group(0)
        if re.search(r"source\s+title\s+list", tag, flags=re.IGNORECASE):
            candidates.append((2, m.group(1), None))
    # 3) Fallback: historic file naming ("ext_list_<Month>_<Year>.xlsx").
    for m in re.finditer(
        r"(?:https?:)?//[^\s\"'<>]+/ext_list[^\s\"'<>]*\.xlsx", text, flags=re.IGNORECASE
    ):
        candidates.append((1, m.group(0), None))
    if not candidates:
        raise LookupError("could not find a Source Title List .xlsx link on the content page")
    candidates.sort(key=lambda c: -c[0])
    _, url, label = candidates[0]
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = httpx.URL(base).join(url).__str__()
    return url, label


def version_label_from(name: str | None) -> str | None:
    if not name:
        return None
    m = re.search(
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s_.-]*(\d{4})",
        name,
        flags=re.IGNORECASE,
    )
    if m:
        return f"{m.group(1).title()} {m.group(2)}"
    m = re.search(r"(20\d{2})[-_]?(\d{2})", name)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def robots_allowed(url: str, user_agent: str, client: httpx.Client) -> bool:
    parsed = httpx.URL(url)
    robots_url = f"{parsed.scheme}://{parsed.host}/robots.txt"
    try:
        resp = client.get(robots_url)
    except httpx.HTTPError:
        return True  # robots.txt unreachable: RFC 9309 treats as allowed
    if resp.status_code >= 400:
        return True
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(resp.text.splitlines())
    return rp.can_fetch(user_agent, url)


# --------------------------------------------------------------------------- parsing


@dataclass
class SourceRow:
    title: str
    issn: str | None
    eissn: str | None
    sourcerecord_id: str | None = None
    coverage: str | None = None
    active: bool | None = None
    discontinued: str | None = None
    source_type: str | None = None
    publisher: str | None = None
    sheet: str = ""


@dataclass
class ParsedList:
    sources: list[SourceRow] = field(default_factory=list)
    proceedings: list[tuple[str, str | None, int | None]] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)
    version_label: str | None = None


def _norm_header(h: Any) -> str:
    return re.sub(r"\s+", " ", str(h or "")).strip().lower()


def _find_col(headers: list[str], *patterns: str) -> int | None:
    for pat in patterns:
        for i, h in enumerate(headers):
            if re.search(pat, h):
                return i
    return None


def _cell(row: tuple[Any, ...], idx: int | None) -> Any:
    if idx is None or idx >= len(row):
        return None
    v = row[idx]
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _parse_table(name: str, rows: Iterator[tuple[Any, ...]], out: ParsedList) -> None:
    """Parse one sheet/CSV. Header row is auto-detected within the first 10 rows."""
    headers: list[str] | None = None
    buffered: list[tuple[Any, ...]] = []
    for _ in range(10):
        try:
            row = next(rows)
        except StopIteration:
            break
        hdr = [_norm_header(c) for c in row]
        joined = " | ".join(hdr)
        if ("title" in joined) and ("issn" in joined or "isbn" in joined):
            headers = hdr
            break
        buffered.append(row)
    if headers is None:
        return
    lname = name.lower()
    c_title = _find_col(headers, r"^source title", r"^title$", r"source title", r"^journal")
    c_issn = _find_col(headers, r"^issn$", r"^print[- ]?issn", r"^p-?issn")
    c_eissn = _find_col(headers, r"^e-?issn$", r"electronic issn")
    c_isbn = _find_col(headers, r"^isbn")
    c_id = _find_col(headers, r"sourcerecord id", r"source ?id")
    c_cov = _find_col(headers, r"^coverage")
    c_active = _find_col(headers, r"active or inactive", r"^status$", r"^active")
    c_disc = _find_col(headers, r"titles discontinued", r"discontinued")
    c_type = _find_col(headers, r"^source type", r"^type$")
    c_pub = _find_col(headers, r"^publisher'?s? name", r"^publisher$", r"^publisher")
    c_year = _find_col(headers, r"^year$")
    c_proc_title = _find_col(headers, r"proceedi?n?ds? title", r"proceedings title")
    if c_title is None:
        return

    is_proceedings = c_isbn is not None and c_issn is None
    is_discontinued_sheet = "discontinued" in lname
    is_accepted_sheet = "accepted" in lname
    if is_accepted_sheet:
        return  # accepted-but-not-yet-indexed titles are not coverage
    out.sheets.append(name)
    for row in rows:
        title = _cell(row, c_title)
        if not title:
            continue
        title = str(title)
        if is_proceedings:
            isbn = normalize_isbn(_cell(row, c_isbn))
            if isbn:
                year = _cell(row, c_year)
                out.proceedings.append(
                    (
                        isbn,
                        title or str(_cell(row, c_proc_title)),
                        int(year) if isinstance(year, int | float) else None,
                    )
                )
            continue
        issn = normalize_issn(_cell(row, c_issn))
        eissn = normalize_issn(_cell(row, c_eissn))
        active_raw = str(_cell(row, c_active) or "").lower()
        active = None
        if active_raw.startswith("active"):
            active = True
        elif active_raw.startswith("inactive"):
            active = False
        disc = _cell(row, c_disc)
        if is_discontinued_sheet:
            disc = disc or "Discontinued"
            year = _cell(row, c_year)
            if isinstance(year, int | float) or (isinstance(year, str) and year.isdigit()):
                disc = f"{disc} ({int(year)})"
            active = False
        sid = _cell(row, c_id)
        out.sources.append(
            SourceRow(
                title=title,
                issn=issn,
                eissn=eissn,
                sourcerecord_id=str(sid) if sid is not None else None,
                coverage=str(_cell(row, c_cov)) if _cell(row, c_cov) is not None else None,
                active=active,
                discontinued=str(disc) if disc else None,
                source_type=_cell(row, c_type),
                publisher=_cell(row, c_pub),
                sheet=name,
            )
        )


def parse_file(path: Path) -> ParsedList:
    out = ParsedList(version_label=version_label_from(path.name))
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                if out.version_label is None:
                    out.version_label = version_label_from(ws.title)
                _parse_table(ws.title, ws.iter_rows(values_only=True), out)
        finally:
            wb.close()
    elif suffix in (".csv", ".tsv", ".txt"):
        with path.open(newline="", encoding="utf-8-sig") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            _parse_table(path.stem, (tuple(r) for r in csv.reader(fh, dialect)), out)
    else:
        raise ValueError(f"unsupported file type {suffix!r}; use .xlsx or .csv")
    if not out.sources:
        raise ValueError(f"no Scopus sources found in {path} (unrecognised layout?)")
    return out


# --------------------------------------------------------------------------- storage


def store(db: Database, parsed: ParsedList, origin: str, sha256: str) -> dict[str, Any]:
    conn = db.conn
    with db.transaction():
        conn.execute("DELETE FROM scopus_source_issns")
        conn.execute("DELETE FROM scopus_sources")
        conn.execute("DELETE FROM scopus_proceedings")
        n_issns = 0
        # Main list rows first so that discontinued-sheet duplicates do not win.
        parsed.sources.sort(
            key=lambda s: ("discontinued" in s.sheet.lower(), "conf" in s.sheet.lower())
        )
        seen_ids: dict[str, int] = {}
        for s in parsed.sources:
            if s.sourcerecord_id and s.sourcerecord_id in seen_ids:
                sid = seen_ids[s.sourcerecord_id]
                if s.discontinued:
                    conn.execute(
                        "UPDATE scopus_sources SET discontinued = COALESCE(discontinued, ?) "
                        "WHERE id=?",
                        (s.discontinued, sid),
                    )
                continue
            cur = conn.execute(
                "INSERT INTO scopus_sources(sourcerecord_id, title, title_norm, issn, eissn, "
                "coverage, active, discontinued, source_type, publisher, sheet) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    s.sourcerecord_id,
                    s.title,
                    normalize_title(s.title),
                    s.issn,
                    s.eissn,
                    s.coverage,
                    None if s.active is None else int(s.active),
                    s.discontinued,
                    s.source_type,
                    s.publisher,
                    s.sheet,
                ),
            )
            sid = cur.lastrowid
            if s.sourcerecord_id:
                seen_ids[s.sourcerecord_id] = sid
            for issn in {s.issn, s.eissn} - {None}:
                conn.execute("INSERT OR IGNORE INTO scopus_source_issns VALUES (?,?)", (issn, sid))
                n_issns += 1
        conn.executemany(
            "INSERT OR IGNORE INTO scopus_proceedings VALUES (?,?,?)", parsed.proceedings
        )
        conn.execute("UPDATE scopus_sources_meta SET active = 0")
        n_sources = conn.execute("SELECT COUNT(*) FROM scopus_sources").fetchone()[0]
        n_isbns = conn.execute("SELECT COUNT(DISTINCT isbn) FROM scopus_proceedings").fetchone()[0]
        conn.execute(
            "INSERT INTO scopus_sources_meta(imported_at, origin, version_label, sha256, "
            "n_sources, n_issns, n_isbns, active) VALUES (?,?,?,?,?,?,?,1)",
            (now_iso(), origin, parsed.version_label, sha256, n_sources, n_issns, n_isbns),
        )
    return {
        "sources": n_sources,
        "issns": n_issns,
        "isbns": n_isbns,
        "version": parsed.version_label,
        "sheets": parsed.sheets,
    }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def import_file(db: Database, path: Path, origin: str | None = None) -> dict[str, Any]:
    parsed = parse_file(path)
    return store(db, parsed, origin or str(path.resolve()), sha256_file(path))


def status(db: Database) -> dict[str, Any] | None:
    row = db.conn.execute(
        "SELECT * FROM scopus_sources_meta WHERE active = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    imported = datetime.fromisoformat(row["imported_at"])
    age_days = (datetime.now(UTC) - imported).days
    return {**dict(row), "age_days": age_days}


def update(
    db: Database,
    settings: Settings,
    *,
    force: bool = False,
    transport: httpx.BaseTransport | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Discover and download the current list from Elsevier if the local copy is stale."""
    current = status(db)
    if current and not force and current["age_days"] * 86400 < settings.ttl.scopus_sources:
        return {
            "skipped": True,
            "reason": f"local list is {current['age_days']} days old "
            f"(< {settings.ttl.scopus_sources // 86400}); "
            "use --force to re-download",
            **current,
        }
    settings.ensure_dirs()
    ua = settings.user_agent()
    with httpx.Client(
        headers={"User-Agent": ua}, timeout=120, follow_redirects=True, transport=transport
    ) as client:
        if not robots_allowed(CONTENT_PAGE, ua, client):
            raise PermissionError(f"robots.txt disallows fetching {CONTENT_PAGE}")
        page = client.get(CONTENT_PAGE)
        page.raise_for_status()
        url, label = discover_download_url(page.text)
        if not robots_allowed(url, ua, client):
            raise PermissionError(f"robots.txt disallows fetching {url}")
        filename = url.rsplit("/", 1)[-1].split("?")[0] or "scopus_source_list.xlsx"
        dest = settings.downloads_dir / filename
        if current and not force and current.get("origin") == url and dest.exists():
            return {"skipped": True, "reason": "already imported this release", **current}
        tmp = dest.with_suffix(dest.suffix + ".part")
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    fh.write(chunk)
        tmp.replace(dest)
    parsed = parse_file(dest)
    parsed.version_label = version_label_from(label) or parsed.version_label
    result = store(db, parsed, url, sha256_file(dest))
    return {"skipped": False, "url": url, "label": label, "file": str(dest), **result}


# --------------------------------------------------------------------------- matching


def parse_coverage(coverage: str | None) -> list[tuple[int, int]]:
    """``"2026; 2023-2024; 1995-2005"`` → ``[(2026, 2026), (2023, 2024), (1995, 2005)]``."""
    ranges = []
    for part in re.split(r"[;,]", coverage or ""):
        m = re.match(r"\s*(\d{4})\s*(?:-\s*(\d{4}|present|ongoing)?)?\s*$", part, flags=re.I)
        if not m:
            continue
        start = int(m.group(1))
        end_raw = m.group(2)
        if end_raw is None and "-" in part:
            end = 9999
        elif end_raw and end_raw.isdigit():
            end = int(end_raw)
        elif end_raw:
            end = 9999
        else:
            end = start
        ranges.append((start, end))
    return ranges


def year_in_coverage(
    year: int | None, coverage: str | None, active: bool | None
) -> tuple[bool | None, str]:
    ranges = parse_coverage(coverage)
    if year is None or not ranges:
        return None, "coverage years not checked"
    for a, b in ranges:
        if a <= year <= b:
            return True, f"{year} within coverage {coverage}"
    latest = max(b for _, b in ranges)
    if active and year > latest:
        # The list lags behind indexing; active titles are normally still being covered.
        return True, (
            f"{year} after last listed coverage year {latest} but source is active "
            "(list lag likely)"
        )
    return False, f"{year} outside coverage {coverage}"


_ORDINAL_RE = re.compile(r"\b\d+(?:st|nd|rd|th)\b|\b(?:19|20)\d{2}\b", re.IGNORECASE)
_ACRONYM_PAREN_RE = re.compile(r"\(([A-Z][A-Za-z0-9&\-]{1,19})(?:\s*'?\d{2,4})?\)")
_ACRONYM_TAIL_RE = re.compile(r",\s*([A-Z][A-Za-z0-9&\-]{1,19})\s+(?:19|20)\d{2}\b")
_SERIES_STOPWORDS = {
    "proceedings",
    "conference",
    "proceeding",
    "ieee",
    "acm",
    "the",
    "of",
    "on",
    "and",
    "in",
    "for",
    "international",
    "annual",
    "symposium",
    "workshop",
}


def conference_acronym(venue: str | None) -> str | None:
    if not venue:
        return None
    m = _ACRONYM_PAREN_RE.search(venue) or _ACRONYM_TAIL_RE.search(venue)
    return m.group(1).upper() if m else None


def edition_year(title: str | None) -> int | None:
    m = re.search(r"\b((?:19|20)\d{2})\b", title or "")
    return int(m.group(1)) if m else None


def series_key(title: str | None) -> str:
    t = _ACRONYM_PAREN_RE.sub(" ", title or "")
    t = t.split(" - ")[0]
    t = _ORDINAL_RE.sub(" ", t)
    t = re.sub(r",\s*[A-Z][A-Za-z0-9&\-]{1,19}\s*$", " ", t.strip().rstrip(","))
    words = [w for w in normalize_title(t).split() if w not in _SERIES_STOPWORDS]
    return " ".join(words)


class SourceIndex:
    def __init__(self, db: Database):
        self.db = db
        self.loaded = status(db) is not None
        self._series: dict[str, list[tuple[str, int | None]]] | None = None

    def _series_index(self) -> dict[str, list[tuple[str, int | None]]]:
        if self._series is None:
            self._series = {}
            for r in self.db.conn.execute("SELECT DISTINCT title, year FROM scopus_proceedings"):
                acr = conference_acronym(r["title"])
                if acr:
                    self._series.setdefault(acr, []).append((r["title"], r["year"]))
        return self._series

    def match_series(
        self, venue: str | None, year: int | None = None
    ) -> tuple[str, int | None, float, bool] | None:
        """Find the conference series of ``venue`` in the Scopus proceedings list.

        Requires the same acronym *and* a similar series name. Returns
        ``(title, year, score, same_edition)``; ``same_edition`` is True when the listed
        proceedings are of the same edition year as ``venue``.
        """
        from rapidfuzz import fuzz

        acr = conference_acronym(venue)
        if not acr:
            return None
        key = series_key(venue)
        wanted = edition_year(venue) or year
        best: tuple[str, int | None, float, bool] | None = None
        for title, pyear in self._series_index().get(acr, []):
            score = float(fuzz.token_set_ratio(key, series_key(title)))
            if score < 85:
                continue
            same = wanted is not None and wanted in (edition_year(title), pyear)
            rank = (same, pyear or 0)
            if best is None or rank > (best[3], best[1] or 0):
                best = (title, pyear, score, same)
        return best

    def by_issn(self, issn: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.conn.execute(
                "SELECT s.* FROM scopus_source_issns i JOIN scopus_sources s ON s.id = i.source_id "
                "WHERE i.issn = ?",
                (issn,),
            )
        ]

    def by_title(self, title: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.conn.execute(
                "SELECT * FROM scopus_sources WHERE title_norm = ?", (normalize_title(title),)
            )
        ]

    def by_isbn(self, isbn: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.conn.execute(
                "SELECT * FROM scopus_proceedings WHERE isbn = ?", (isbn,)
            )
        ]

    def match(self, work: WorkRecord) -> SourceMatch:
        if not self.loaded:
            return SourceMatch(
                status=ScopusSourceStatus.LIST_NOT_LOADED,
                evidence=[
                    "Scopus Source List not loaded; run `citation-reconciler scopus-sources update`"
                ],
            )
        evidence: list[str] = []
        for issn in work.issns:
            rows = self.by_issn(issn)
            if not rows:
                continue
            # Prefer the active record when an ISSN maps to several (title history).
            rows.sort(key=lambda r: (r["active"] != 1, r["discontinued"] is not None))
            r = rows[0]
            active = None if r["active"] is None else bool(r["active"])
            evidence.append(
                f"ISSN {issn} matches Scopus source '{r['title']}' "
                f"(id {r['sourcerecord_id']}, "
                f"{'active' if active else 'inactive' if active is False else '?'})"
            )
            inside, why = year_in_coverage(work.year, r["coverage"], active)
            if r["discontinued"]:
                evidence.append(f"source flagged: {r['discontinued']}")
                m = re.search(r"\b((?:19|20)\d{2})\b", str(r["discontinued"]))
                if m and work.year and work.year > int(m.group(1)):
                    inside, why = False, f"{work.year} is after discontinuation ({m.group(1)})"
            evidence.append(why)
            st = (
                ScopusSourceStatus.OUTSIDE_COVERAGE
                if inside is False
                else ScopusSourceStatus.COVERED
            )
            return SourceMatch(
                status=st,
                source_title=r["title"],
                sourcerecord_id=r["sourcerecord_id"],
                matched_on=f"issn:{issn}",
                active=active,
                coverage=r["coverage"],
                evidence=evidence,
            )
        for isbn in work.isbns:
            rows = self.by_isbn(isbn)
            if rows:
                r = rows[0]
                evidence.append(
                    f"ISBN {isbn} matches Scopus-indexed proceedings '{r['title']}' ({r['year']})"
                )
                return SourceMatch(
                    status=ScopusSourceStatus.COVERED,
                    source_title=r["title"],
                    matched_on=f"isbn:{isbn}",
                    evidence=evidence,
                )
        if work.issns:
            evidence.append(f"ISSN(s) {', '.join(work.issns)} not in Scopus Source List")
            return SourceMatch(status=ScopusSourceStatus.NOT_IN_LIST, evidence=evidence)
        # Title fallback only when no ISSN is known.
        if work.venue and len(normalize_title(work.venue)) >= 6:
            rows = self.by_title(work.venue)
            if len(rows) == 1:
                r = rows[0]
                active = None if r["active"] is None else bool(r["active"])
                inside, why = year_in_coverage(work.year, r["coverage"], active)
                evidence += [
                    f"no ISSN; venue title '{work.venue}' exactly matches Scopus source "
                    f"'{r['title']}' (normalised title match only)",
                    why,
                ]
                return SourceMatch(
                    status=(
                        ScopusSourceStatus.OUTSIDE_COVERAGE
                        if inside is False
                        else ScopusSourceStatus.COVERED_TITLE_MATCH
                    ),
                    source_title=r["title"],
                    sourcerecord_id=r["sourcerecord_id"],
                    matched_on="title",
                    active=active,
                    coverage=r["coverage"],
                    evidence=evidence,
                )
            if len(rows) > 1:
                evidence.append(
                    f"venue title '{work.venue}' matches {len(rows)} Scopus sources (ambiguous)"
                )
                return SourceMatch(status=ScopusSourceStatus.NO_SOURCE_ID, evidence=evidence)
        if work.isbns:
            evidence.append(f"ISBN(s) {', '.join(work.isbns)} not in Scopus proceedings list")
        series = self.match_series(work.venue, work.year)
        if series:
            title, year, score, same_edition = series
            if same_edition:
                evidence.append(
                    f"no ISSN/ISBN; this conference edition is listed in the Scopus proceedings "
                    f"list as '{title}' ({year}) (matched by acronym + name similarity "
                    f"{score:.0f}, not by identifier)"
                )
                return SourceMatch(
                    status=ScopusSourceStatus.COVERED_TITLE_MATCH,
                    source_title=title,
                    matched_on="conference_title",
                    evidence=evidence,
                )
            evidence += [
                "no ISSN/ISBN; conference series heuristic: an earlier edition "
                f"'{title}' ({year}) is in the Scopus proceedings list "
                f"(acronym + name similarity {score:.0f})",
                "this does NOT prove the current edition is indexed",
            ]
            return SourceMatch(
                status=ScopusSourceStatus.SERIES_PREVIOUSLY_INDEXED,
                source_title=title,
                matched_on="conference_series",
                evidence=evidence,
            )
        evidence.append("no ISSN available to match against the Scopus Source List")
        return SourceMatch(status=ScopusSourceStatus.NO_SOURCE_ID, evidence=evidence)

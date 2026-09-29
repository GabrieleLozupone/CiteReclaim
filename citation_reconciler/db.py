"""SQLite persistence (stdlib ``sqlite3``; no ORM)."""

from __future__ import annotations

import gzip
import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ReconciliationResult, TargetClassification, TrackedPaper, WorkRecord

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Tracked papers (the works whose citations are reconciled).
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    title TEXT,
    authors TEXT,               -- JSON list
    arxiv_id TEXT,
    arxiv_doi TEXT,
    journal_doi TEXT,
    journal_title TEXT,
    journal_issns TEXT,          -- JSON list
    volume TEXT,
    pages TEXT,
    year INTEGER,
    created_at TEXT,
    updated_at TEXT
);

-- Provider records for each version of a tracked paper.
CREATE TABLE IF NOT EXISTS paper_versions (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    version TEXT NOT NULL,       -- 'preprint' | 'vor'
    provider TEXT NOT NULL,      -- 'openalex' | 'semantic_scholar' | 'crossref' | 'scopus'
    provider_id TEXT NOT NULL,
    title TEXT,
    detail TEXT,
    PRIMARY KEY (paper_id, version, provider, provider_id)
);

-- Canonical citing works.
CREATE TABLE IF NOT EXISTS works (
    id INTEGER PRIMARY KEY,
    doi TEXT UNIQUE,
    arxiv_id TEXT,
    pmid TEXT,
    openalex_id TEXT,
    s2_id TEXT,
    scopus_eid TEXT,
    title TEXT,
    title_norm TEXT,
    authors TEXT,
    year INTEGER,
    venue TEXT,
    issns TEXT,
    isbns TEXT,
    publisher TEXT,
    publication_type TEXT,
    record TEXT,                 -- full merged WorkRecord JSON
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS works_title_norm ON works(title_norm);

-- Venues seen on citing works (ISSN-keyed cache of source metadata).
CREATE TABLE IF NOT EXISTS venues (
    issn TEXT PRIMARY KEY,
    title TEXT,
    publisher TEXT,
    updated_at TEXT
);

-- A citing work -> tracked paper relation with the reconciliation outcome.
CREATE TABLE IF NOT EXISTS citations (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    target_version TEXT,
    target_evidence TEXT,        -- JSON
    matched_reference TEXT,
    result TEXT,                 -- ReconciliationResult JSON
    action TEXT,
    confidence REAL,
    first_seen TEXT,
    last_seen TEXT,
    UNIQUE (paper_id, work_id)
);

-- Which provider/channel reported each citation.
CREATE TABLE IF NOT EXISTS citation_sources (
    citation_id INTEGER NOT NULL REFERENCES citations(id) ON DELETE CASCADE,
    source TEXT NOT NULL,        -- openalex | semantic_scholar | scopus | google_scholar_import
    against TEXT,                -- tracked-paper record it was reported for (openalex:W123)
    first_seen TEXT,
    last_seen TEXT,
    PRIMARY KEY (citation_id, source, against)
);

-- Bibliography entries of citing works (raw, for evidence / debugging).
CREATE TABLE IF NOT EXISTS "references" (
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    source TEXT,
    doi TEXT,
    entry TEXT,                  -- JSON ReferenceEntry
    PRIMARY KEY (work_id, position)
);

-- Scopus Source Title List (normalised).
CREATE TABLE IF NOT EXISTS scopus_sources (
    id INTEGER PRIMARY KEY,
    sourcerecord_id TEXT,
    title TEXT,
    title_norm TEXT,
    issn TEXT,
    eissn TEXT,
    coverage TEXT,
    active INTEGER,              -- 1 active / 0 inactive / NULL unknown
    discontinued TEXT,
    source_type TEXT,
    publisher TEXT,
    sheet TEXT
);
CREATE TABLE IF NOT EXISTS scopus_source_issns (
    issn TEXT NOT NULL,
    source_id INTEGER NOT NULL REFERENCES scopus_sources(id) ON DELETE CASCADE,
    PRIMARY KEY (issn, source_id)
);
CREATE INDEX IF NOT EXISTS scopus_sources_title ON scopus_sources(title_norm);
CREATE TABLE IF NOT EXISTS scopus_proceedings (
    isbn TEXT NOT NULL,
    title TEXT,
    year INTEGER,
    PRIMARY KEY (isbn, title, year)
);
CREATE TABLE IF NOT EXISTS scopus_sources_meta (
    id INTEGER PRIMARY KEY,
    imported_at TEXT,
    origin TEXT,                 -- URL or local file path
    version_label TEXT,
    sha256 TEXT,
    n_sources INTEGER,
    n_issns INTEGER,
    n_isbns INTEGER,
    active INTEGER DEFAULT 1
);

-- HTTP response cache (bodies gzip-compressed).
CREATE TABLE IF NOT EXISTS api_cache (
    key TEXT PRIMARY KEY,
    provider TEXT,
    url TEXT,
    status INTEGER,
    body BLOB,
    fetched_at REAL,
    expires_at REAL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER REFERENCES papers(id) ON DELETE CASCADE,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    stats TEXT,                  -- JSON
    warnings TEXT                -- JSON list
);

CREATE TABLE IF NOT EXISTS manual_reviews (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER REFERENCES papers(id) ON DELETE CASCADE,
    work_id INTEGER REFERENCES works(id) ON DELETE CASCADE,
    reason TEXT,
    created_at TEXT,
    resolved INTEGER DEFAULT 0,
    UNIQUE (paper_id, work_id, reason)
);

-- Citations added by hand after checking them (e.g. in Scopus), with the cited version.
CREATE TABLE IF NOT EXISTS manual_citations (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    doi TEXT NOT NULL,
    target TEXT NOT NULL,        -- PREPRINT | VERSION_OF_RECORD | BOTH
    note TEXT,
    created_at TEXT,
    PRIMARY KEY (paper_id, doi)
);

-- Rows imported from Google Scholar exports (kept verbatim).
CREATE TABLE IF NOT EXISTS scholar_imports (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    file TEXT,
    row_hash TEXT,
    row TEXT,                    -- JSON
    imported_at TEXT,
    UNIQUE (paper_id, row_hash)
);
"""


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path) if str(path) != ":memory:" else path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL") if str(path) != ":memory:" else None
        self.init()

    def init(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------ meta
    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, _j(value)))
        self.conn.commit()

    def get_meta(self, key: str) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    # ------------------------------------------------------------------ cache
    def cache_get(self, key: str) -> tuple[int, bytes, float] | None:
        row = self.conn.execute(
            "SELECT status, body, expires_at, fetched_at FROM api_cache WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return None
        return row["status"], gzip.decompress(row["body"]), row["expires_at"]

    def cache_put(
        self, key: str, provider: str, url: str, status: int, body: bytes, ttl: float
    ) -> None:
        now = time.time()
        self.conn.execute(
            "INSERT OR REPLACE INTO api_cache(key, provider, url, status, body, fetched_at, "
            "expires_at) VALUES (?,?,?,?,?,?,?)",
            (key, provider, url, status, gzip.compress(body), now, now + ttl),
        )
        self.conn.commit()

    def cache_stats(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT provider, COUNT(*) n, SUM(expires_at > ?) fresh FROM api_cache "
            "GROUP BY provider",
            (time.time(),),
        ).fetchall()

    def cache_clear(self, provider: str | None = None) -> int:
        cur = self.conn.execute(
            "DELETE FROM api_cache" + (" WHERE provider = ?" if provider else ""),
            (provider,) if provider else (),
        )
        self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------ papers
    def upsert_paper(self, p: TrackedPaper) -> TrackedPaper:
        ts = now_iso()
        existing = self.get_paper(p.name)
        values = (
            p.title,
            _j(p.authors),
            p.arxiv_id,
            p.arxiv_doi,
            p.journal_doi,
            p.journal_title,
            _j(p.journal_issns),
            p.volume,
            p.pages,
            p.year,
        )
        if existing:
            self.conn.execute(
                "UPDATE papers SET title=?, authors=?, arxiv_id=?, arxiv_doi=?, journal_doi=?, "
                "journal_title=?, journal_issns=?, volume=?, pages=?, year=?, updated_at=? "
                "WHERE id=?",
                (*values, ts, existing.id),
            )
        else:
            self.conn.execute(
                "INSERT INTO papers(name, title, authors, arxiv_id, arxiv_doi, journal_doi, "
                "journal_title, journal_issns, volume, pages, year, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p.name, *values, ts, ts),
            )
        self.conn.commit()
        out = self.get_paper(p.name)
        assert out is not None
        return out

    def _paper_from_row(self, row: sqlite3.Row) -> TrackedPaper:
        versions: dict[str, dict[str, str]] = {}
        for v in self.conn.execute(
            "SELECT version, provider, provider_id FROM paper_versions WHERE paper_id=?",
            (row["id"],),
        ):
            versions.setdefault(v["version"], {})
            key = v["provider"]
            # Several OpenAlex works may exist for one version; keep them all.
            if key in versions[v["version"]]:
                versions[v["version"]][key] += "|" + v["provider_id"]
            else:
                versions[v["version"]][key] = v["provider_id"]
        return TrackedPaper(
            id=row["id"],
            name=row["name"],
            title=row["title"],
            authors=json.loads(row["authors"] or "[]"),
            arxiv_id=row["arxiv_id"],
            arxiv_doi=row["arxiv_doi"],
            journal_doi=row["journal_doi"],
            journal_title=row["journal_title"],
            journal_issns=json.loads(row["journal_issns"] or "[]"),
            volume=row["volume"],
            pages=row["pages"],
            year=row["year"],
            version_ids=versions,
        )

    def get_paper(self, name: str) -> TrackedPaper | None:
        row = self.conn.execute(
            "SELECT * FROM papers WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        return self._paper_from_row(row) if row else None

    def list_papers(self) -> list[TrackedPaper]:
        rows = self.conn.execute("SELECT * FROM papers ORDER BY name").fetchall()
        return [self._paper_from_row(r) for r in rows]

    def delete_paper(self, name: str) -> bool:
        cur = self.conn.execute("DELETE FROM papers WHERE name = ? COLLATE NOCASE", (name,))
        self.conn.commit()
        return cur.rowcount > 0

    def set_paper_versions(
        self, paper_id: int, rows: Iterable[tuple[str, str, str, str | None, str | None]]
    ) -> None:
        self.conn.execute("DELETE FROM paper_versions WHERE paper_id=?", (paper_id,))
        self.conn.executemany(
            "INSERT OR IGNORE INTO paper_versions(paper_id, version, provider, provider_id, "
            "title, detail) VALUES (?,?,?,?,?,?)",
            [(paper_id, *r) for r in rows],
        )
        self.conn.commit()

    # ------------------------------------------------------------------ works / citations
    def find_work_id(self, rec: WorkRecord) -> int | None:
        ids = rec.ids
        for col, val in (
            ("doi", ids.doi),
            ("pmid", ids.pmid),
            ("arxiv_id", ids.arxiv),
            ("s2_id", ids.s2),
            ("openalex_id", ids.openalex),
        ):
            if val:
                row = self.conn.execute(f"SELECT id FROM works WHERE {col} = ?", (val,)).fetchone()
                if row:
                    return row["id"]
        return None

    def upsert_work(self, rec: WorkRecord) -> int:
        from .matching import normalize_title

        ts = now_iso()
        values = (
            rec.ids.doi,
            rec.ids.arxiv,
            rec.ids.pmid,
            rec.ids.openalex,
            rec.ids.s2,
            rec.ids.scopus_eid,
            rec.title,
            normalize_title(rec.title),
            _j(rec.authors),
            rec.year,
            rec.venue,
            _j(rec.issns),
            _j(rec.isbns),
            rec.publisher,
            rec.publication_type.value,
            rec.model_dump_json(),
            ts,
        )
        work_id = self.find_work_id(rec)
        if work_id:
            self.conn.execute(
                "UPDATE works SET doi=?, arxiv_id=?, pmid=?, openalex_id=?, s2_id=?, scopus_eid=?, "
                "title=?, title_norm=?, authors=?, year=?, venue=?, issns=?, isbns=?, publisher=?, "
                "publication_type=?, record=?, updated_at=? WHERE id=?",
                (*values, work_id),
            )
        else:
            cur = self.conn.execute(
                "INSERT INTO works(doi, arxiv_id, pmid, openalex_id, s2_id, scopus_eid, title, "
                "title_norm, authors, year, venue, issns, isbns, publisher, publication_type, "
                "record, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            work_id = cur.lastrowid
        if rec.references is not None:
            self.conn.execute('DELETE FROM "references" WHERE work_id=?', (work_id,))
            self.conn.executemany(
                'INSERT INTO "references"(work_id, position, source, doi, entry) '
                "VALUES (?,?,?,?,?)",
                [
                    (work_id, i, r.source, r.doi, r.model_dump_json())
                    for i, r in enumerate(rec.references)
                ],
            )
        for issn in rec.issns:
            self.conn.execute(
                "INSERT INTO venues(issn, title, publisher, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(issn) DO UPDATE SET title=excluded.title, "
                "updated_at=excluded.updated_at",
                (issn, rec.venue, rec.publisher, ts),
            )
        assert work_id is not None
        return work_id

    def upsert_citation(
        self,
        paper_id: int,
        work_id: int,
        target: TargetClassification,
        result: ReconciliationResult,
        sources: list[tuple[str, str]],
    ) -> int:
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO citations(paper_id, work_id, target_version, target_evidence, "
            "matched_reference, result, action, confidence, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(paper_id, work_id) DO UPDATE SET "
            "target_version=excluded.target_version, target_evidence=excluded.target_evidence, "
            "matched_reference=excluded.matched_reference, result=excluded.result, "
            "action=excluded.action, confidence=excluded.confidence, last_seen=excluded.last_seen",
            (
                paper_id,
                work_id,
                target.target_version.value,
                target.model_dump_json(),
                target.matched_reference,
                result.model_dump_json(),
                result.action.value,
                result.confidence,
                ts,
                ts,
            ),
        )
        cid = self.conn.execute(
            "SELECT id FROM citations WHERE paper_id=? AND work_id=?", (paper_id, work_id)
        ).fetchone()["id"]
        for source, against in sources:
            self.conn.execute(
                "INSERT INTO citation_sources(citation_id, source, against, first_seen, last_seen) "
                "VALUES (?,?,?,?,?) ON CONFLICT DO UPDATE SET last_seen=excluded.last_seen",
                (cid, source, against or "", ts, ts),
            )
        return cid

    def prune_citations(self, paper_id: int, keep_work_ids: set[int]) -> int:
        """Drop citations of ``paper_id`` that were not seen in the latest sync."""
        rows = self.conn.execute(
            "SELECT id, work_id FROM citations WHERE paper_id=?", (paper_id,)
        ).fetchall()
        stale = [r["id"] for r in rows if r["work_id"] not in keep_work_ids]
        self.conn.executemany("DELETE FROM citations WHERE id=?", [(s,) for s in stale])
        self.conn.commit()
        return len(stale)

    def citations_for_paper(self, paper_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT c.*, w.record AS work_record, w.id AS wid FROM citations c "
            "JOIN works w ON w.id = c.work_id WHERE c.paper_id=? ORDER BY w.year DESC, w.title",
            (paper_id,),
        ).fetchall()
        out = []
        for r in rows:
            srcs = self.conn.execute(
                "SELECT source, against FROM citation_sources WHERE citation_id=?", (r["id"],)
            ).fetchall()
            out.append(
                {
                    "citation_id": r["id"],
                    "work_id": r["wid"],
                    "work": WorkRecord.model_validate_json(r["work_record"]),
                    "target": TargetClassification.model_validate_json(r["target_evidence"]),
                    "result": ReconciliationResult.model_validate_json(r["result"]),
                    "sources": sorted({s["source"] for s in srcs}),
                    "against": sorted({s["against"] for s in srcs if s["against"]}),
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                }
            )
        return out

    # ------------------------------------------------------------------ reviews / runs
    def add_review(self, paper_id: int, work_id: int, reason: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO manual_reviews(paper_id, work_id, reason, created_at) "
            "VALUES (?,?,?,?)",
            (paper_id, work_id, reason, now_iso()),
        )

    def reviews_for_paper(self, paper_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT m.*, w.title FROM manual_reviews m LEFT JOIN works w ON w.id = m.work_id "
            "WHERE m.paper_id=? AND m.resolved=0",
            (paper_id,),
        ).fetchall()

    def start_run(self, paper_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO sync_runs(paper_id, started_at, status) VALUES (?,?, 'running')",
            (paper_id, now_iso()),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def finish_run(
        self, run_id: int, status: str, stats: dict[str, Any], warnings: list[str]
    ) -> None:
        self.conn.execute(
            "UPDATE sync_runs SET finished_at=?, status=?, stats=?, warnings=? WHERE id=?",
            (now_iso(), status, _j(stats), _j(warnings), run_id),
        )
        self.conn.commit()

    def last_run(self, paper_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sync_runs WHERE paper_id=? ORDER BY id DESC LIMIT 1", (paper_id,)
        ).fetchone()

    # ------------------------------------------------------------------ scholar imports
    def add_scholar_rows(
        self, paper_id: int, file: str, rows: list[tuple[str, dict[str, Any]]]
    ) -> int:
        n = 0
        for row_hash, row in rows:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO scholar_imports(paper_id, file, row_hash, row, imported_at) "
                "VALUES (?,?,?,?,?)",
                (paper_id, file, row_hash, _j(row), now_iso()),
            )
            n += cur.rowcount
        self.conn.commit()
        return n

    def add_manual_citation(self, paper_id: int, doi: str, target: str, note: str | None) -> None:
        self.conn.execute(
            "INSERT INTO manual_citations(paper_id, doi, target, note, created_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(paper_id, doi) DO UPDATE SET "
            "target=excluded.target, note=excluded.note",
            (paper_id, doi, target, note, now_iso()),
        )
        self.conn.commit()

    def remove_manual_citation(self, paper_id: int, doi: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM manual_citations WHERE paper_id=? AND doi=?", (paper_id, doi)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def manual_citations(self, paper_id: int) -> dict[str, dict[str, Any]]:
        return {
            r["doi"]: dict(r)
            for r in self.conn.execute(
                "SELECT * FROM manual_citations WHERE paper_id=? ORDER BY created_at", (paper_id,)
            )
        }

    def scholar_rows(self, paper_id: int) -> list[dict[str, Any]]:
        return [
            json.loads(r["row"])
            for r in self.conn.execute(
                "SELECT row FROM scholar_imports WHERE paper_id=? ORDER BY id", (paper_id,)
            )
        ]

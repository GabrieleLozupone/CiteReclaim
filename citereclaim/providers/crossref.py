"""Crossref REST API adapter (https://api.crossref.org). No key required."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..config import Settings
from ..db import Database
from ..matching import (
    normalize_arxiv_id,
    normalize_doi,
    normalize_isbn,
    normalize_issn,
    title_similarity,
)
from ..models import ExternalIds, PublicationType, ReferenceEntry, WorkRecord
from .base import HttpClient, ProviderError, Response

BASE = "https://api.crossref.org"

TYPE_MAP = {
    "journal-article": PublicationType.JOURNAL_ARTICLE,
    "proceedings-article": PublicationType.CONFERENCE_PAPER,
    "posted-content": PublicationType.PREPRINT,
    "dissertation": PublicationType.THESIS,
    "book": PublicationType.BOOK,
    "monograph": PublicationType.BOOK,
    "edited-book": PublicationType.BOOK,
    "reference-book": PublicationType.BOOK,
    "book-chapter": PublicationType.BOOK_CHAPTER,
    "book-section": PublicationType.BOOK_CHAPTER,
    "book-part": PublicationType.BOOK_CHAPTER,
}


def _strip_tags(s: str | None) -> str | None:
    """Drop JATS/HTML tags and normalise odd whitespace (e.g. non-breaking spaces)."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip() if s else s


def _year(msg: dict[str, Any]) -> int | None:
    for k in ("published-print", "published-online", "issued", "published", "created"):
        parts = (msg.get(k) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            return int(parts[0][0])
    return None


def parse_reference(ref: dict[str, Any]) -> ReferenceEntry:
    return ReferenceEntry(
        doi=normalize_doi(ref.get("DOI")),
        doi_asserted_by=ref.get("doi-asserted-by"),
        unstructured=ref.get("unstructured"),
        article_title=ref.get("article-title"),
        journal_title=ref.get("journal-title")
        or ref.get("series-title")
        or ref.get("volume-title"),
        volume=str(ref["volume"]) if ref.get("volume") else None,
        first_page=str(ref["first-page"]) if ref.get("first-page") else None,
        year=str(ref["year"]) if ref.get("year") else None,
        author=ref.get("author"),
        source="crossref",
    )


def parse_work(msg: dict[str, Any]) -> WorkRecord:
    doi = normalize_doi(msg.get("DOI"))
    ctype = msg.get("type", "")
    ptype = TYPE_MAP.get(ctype, PublicationType.UNKNOWN)
    if ctype == "posted-content" and msg.get("subtype") not in (None, "preprint"):
        ptype = PublicationType.UNKNOWN
    authors = []
    for a in msg.get("author") or []:
        name = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name")
        if name:
            authors.append(name)
    issns = []
    for i in msg.get("issn-type") or []:
        n = normalize_issn(i.get("value"))
        if n and n not in issns:
            issns.append(n)
    for i in msg.get("ISSN") or []:
        n = normalize_issn(i)
        if n and n not in issns:
            issns.append(n)
    isbns = []
    for i in (msg.get("ISBN") or []) + [x.get("value") for x in msg.get("isbn-type") or []]:
        n = normalize_isbn(i)
        if n and n not in isbns:
            isbns.append(n)
    container = (msg.get("container-title") or [None])[0]
    if ptype == PublicationType.PREPRINT and not container and msg.get("institution"):
        container = msg["institution"][0].get("name")
    arxiv = normalize_arxiv_id(doi) if doi else None
    refs = msg.get("reference")
    relation = msg.get("relation") or {}
    rec = WorkRecord(
        ids=ExternalIds(doi=doi, arxiv=arxiv),
        title=_strip_tags((msg.get("title") or [None])[0]),
        authors=authors,
        year=_year(msg),
        venue=container,
        issns=issns,
        isbns=isbns,
        publisher=msg.get("publisher"),
        publication_type=ptype,
        raw_types={"crossref": ctype},
        sources=["crossref"],
        references=[parse_reference(r) for r in refs] if refs is not None else None,
        extra={
            "crossref_volume": msg.get("volume"),
            "crossref_page": msg.get("page") or msg.get("article-number"),
            "crossref_relation": {
                k: [normalize_doi(x.get("id")) or x.get("id") for x in v]
                for k, v in relation.items()
                if k in ("has-preprint", "is-preprint-of", "has-version", "is-version-of")
            },
            "references_count": msg.get("references-count") or msg.get("reference-count"),
        },
    )
    return rec


class CrossrefClient:
    name = "crossref"

    def __init__(self, settings: Settings, db: Database, **http_kwargs: Any):
        polite = bool(settings.crossref_mailto)
        self.settings = settings
        self.http = HttpClient(
            self.name, settings, db, min_interval=0.12 if polite else 0.25, **http_kwargs
        )

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(extra or {})
        if self.settings.crossref_mailto:
            params["mailto"] = self.settings.crossref_mailto
        return params

    def get_work(self, doi: str) -> WorkRecord | None:
        doi_n = normalize_doi(doi)
        if not doi_n:
            return None
        resp: Response = self.http.get(
            f"{BASE}/works/{quote(doi_n, safe='/')}",
            self._params(),
            ttl=self.settings.ttl.metadata,
        )
        if resp.status != 200:
            return None
        return parse_work(resp.json()["message"])

    def search_bibliographic(
        self, title: str, authors: str | None = None, year: int | None = None, rows: int = 5
    ) -> list[WorkRecord]:
        query = " ".join(x for x in (title, authors, str(year) if year else None) if x)
        params = self._params(
            {
                "query.bibliographic": query,
                "rows": rows,
                "select": "DOI,title,author,issued,published-print,"
                "published-online,container-title,ISSN,issn-type,type,"
                "publisher,ISBN",
            }
        )
        resp = self.http.get(f"{BASE}/works", params, ttl=self.settings.ttl.search)
        if resp.status == 404:
            return []
        if resp.status != 200:
            raise ProviderError(self.name, f"search failed: HTTP {resp.status}", resp.status)
        return [parse_work(i) for i in resp.json()["message"].get("items", [])]

    def resolve_title(
        self, title: str, authors: list[str] | None = None, year: int | None = None
    ) -> tuple[WorkRecord | None, float]:
        """Best Crossref match for a title, with similarity score (0–100)."""
        from ..matching import first_author_matches

        first = authors[0] if authors else None
        best, best_score = None, 0.0
        for cand in self.search_bibliographic(title, first, year):
            score = title_similarity(title, cand.title)
            if year and cand.year and abs(cand.year - year) > 1:
                score -= 10
            if authors and cand.authors and first_author_matches(authors, cand.authors):
                score += 2
            if score > best_score:
                best, best_score = cand, score
        return best, min(best_score, 100.0)

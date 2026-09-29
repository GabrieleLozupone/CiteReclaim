"""Semantic Scholar Academic Graph API adapter. API key optional."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..config import Settings
from ..db import Database
from ..matching import is_preprint_doi, normalize_arxiv_id, normalize_doi, normalize_issn
from ..models import ExternalIds, PublicationType, WorkRecord
from .base import HttpClient

BASE = "https://api.semanticscholar.org/graph/v1"

PAPER_FIELDS = (
    "paperId,externalIds,title,year,venue,publicationTypes,journal,"
    "publicationVenue,authors,citationCount"
)
CITATION_FIELDS = (
    "contexts,intents,paperId,externalIds,title,year,venue,publicationTypes,"
    "journal,publicationVenue,authors"
)


def _ptype(types: list[str] | None, venue: str | None, doi: str | None) -> PublicationType:
    t = set(types or [])
    v = (venue or "").lower()
    if "JournalArticle" in t and "arxiv" not in v:
        return PublicationType.JOURNAL_ARTICLE
    if "Conference" in t:
        return PublicationType.CONFERENCE_PAPER
    if "BookSection" in t:
        return PublicationType.BOOK_CHAPTER
    if "Book" in t:
        return PublicationType.BOOK
    if (
        "arxiv" in v
        or v in ("biorxiv", "medrxiv", "ssrn", "research square")
        or (doi and is_preprint_doi(doi) and not t & {"JournalArticle", "Conference"})
    ):
        return PublicationType.PREPRINT
    if "Review" in t or "JournalArticle" in t:
        return PublicationType.JOURNAL_ARTICLE
    return PublicationType.UNKNOWN


def parse_paper(p: dict[str, Any]) -> WorkRecord:
    ext = p.get("externalIds") or {}
    doi = normalize_doi(ext.get("DOI"))
    arxiv = normalize_arxiv_id(ext.get("ArXiv"))
    journal = p.get("journal") or {}
    pvenue = p.get("publicationVenue") or {}
    issns = [i for i in (normalize_issn(pvenue.get("issn")),) if i]
    venue = journal.get("name") or pvenue.get("name") or p.get("venue") or None
    ptypes = p.get("publicationTypes") or []
    rec = WorkRecord(
        ids=ExternalIds(
            doi=doi, arxiv=arxiv, pmid=ext.get("PubMed"), s2=p.get("paperId"), dblp=ext.get("DBLP")
        ),
        title=p.get("title"),
        authors=[a.get("name") for a in p.get("authors") or [] if a.get("name")],
        year=p.get("year"),
        venue=venue,
        issns=issns,
        publication_type=_ptype(ptypes, venue or p.get("venue"), doi),
        raw_types={"semantic_scholar": ",".join(ptypes) or (p.get("venue") or "")},
        sources=["semantic_scholar"],
    )
    if doi and not arxiv and is_preprint_doi(doi):
        rec.ids.arxiv = normalize_arxiv_id(doi)
    return rec


class SemanticScholarClient:
    name = "semantic_scholar"

    def __init__(self, settings: Settings, db: Database, **http_kwargs: Any):
        self.settings = settings
        headers = {}
        if settings.semantic_scholar_api_key:
            headers["x-api-key"] = settings.semantic_scholar_api_key
        # Anonymous traffic shares one pool and is throttled; be conservative.
        http_kwargs.setdefault("max_retries", 6 if not headers else 4)
        http_kwargs.setdefault("backoff_base", 2.0)
        self.http = HttpClient(
            self.name, settings, db, min_interval=1.1, headers=headers, **http_kwargs
        )

    def get_paper(self, paper_id: str) -> WorkRecord | None:
        """``paper_id`` may be an S2 id, ``DOI:...``, ``ARXIV:...``, ``PMID:...``."""
        resp = self.http.get(
            f"{BASE}/paper/{quote(paper_id, safe=':/')}",
            {"fields": PAPER_FIELDS},
            ttl=self.settings.ttl.metadata,
        )
        if resp.status != 200:
            return None
        return parse_paper(resp.json())

    def get_by_doi(self, doi: str) -> WorkRecord | None:
        return self.get_paper(f"DOI:{doi}")

    def get_by_arxiv(self, arxiv_id: str) -> WorkRecord | None:
        return self.get_paper(f"ARXIV:{arxiv_id}")

    def match_title(self, title: str) -> WorkRecord | None:
        resp = self.http.get(
            f"{BASE}/paper/search/match",
            {"query": title, "fields": PAPER_FIELDS},
            ttl=self.settings.ttl.search,
        )
        if resp.status != 200:
            return None
        data = (resp.json() or {}).get("data") or []
        return parse_paper(data[0]) if data else None

    def citations(self, paper_id: str, limit: int = 500) -> list[WorkRecord]:
        out: list[WorkRecord] = []
        offset = 0
        while True:
            resp = self.http.get(
                f"{BASE}/paper/{quote(paper_id, safe=':/')}/citations",
                {"fields": CITATION_FIELDS, "limit": limit, "offset": offset},
                ttl=self.settings.ttl.citations,
            )
            if resp.status != 200:
                break
            payload = resp.json() or {}
            for item in payload.get("data") or []:
                citing = item.get("citingPaper") or {}
                if not citing.get("paperId") and not citing.get("title"):
                    continue
                rec = parse_paper(citing)
                rec.s2_contexts = [c for c in item.get("contexts") or [] if c]
                rec.discovered_against.append(f"semantic_scholar:{paper_id}")
                out.append(rec)
            nxt = payload.get("next")
            if nxt is None or nxt <= offset:
                break
            offset = nxt
        return out

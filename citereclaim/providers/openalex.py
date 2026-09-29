"""OpenAlex adapter.

Since February 2026 OpenAlex uses usage-based pricing: singleton lookups by ID/DOI are
free, list/filter calls cost credits. Without a key a small daily testing budget applies;
a free key raises it (see README). The key is sent as the ``api_key`` query parameter.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..db import Database
from ..matching import (
    is_preprint_doi,
    normalize_arxiv_id,
    normalize_doi,
    normalize_isbn,
    normalize_issn,
)
from ..models import ExternalIds, PublicationType, WorkRecord
from .base import HttpClient

BASE = "https://api.openalex.org"

SELECT = (
    "id,doi,ids,title,display_name,publication_year,type,type_crossref,primary_location,"
    "locations,authorships,biblio"
)

TYPE_MAP = {
    "article": PublicationType.JOURNAL_ARTICLE,
    "review": PublicationType.JOURNAL_ARTICLE,
    "letter": PublicationType.JOURNAL_ARTICLE,
    "editorial": PublicationType.JOURNAL_ARTICLE,
    "preprint": PublicationType.PREPRINT,
    "posted-content": PublicationType.PREPRINT,
    "dissertation": PublicationType.THESIS,
    "book": PublicationType.BOOK,
    "book-chapter": PublicationType.BOOK_CHAPTER,
    "conference-paper": PublicationType.CONFERENCE_PAPER,
    "proceedings-article": PublicationType.CONFERENCE_PAPER,
}


def short_id(openalex_id: str | None) -> str | None:
    if not openalex_id:
        return None
    return openalex_id.rstrip("/").rsplit("/", 1)[-1]


def _arxiv_from_locations(w: dict[str, Any]) -> str | None:
    for loc in w.get("locations") or []:
        for key in ("landing_page_url", "pdf_url"):
            url = loc.get(key) or ""
            if "arxiv.org" in url:
                aid = normalize_arxiv_id(url)
                if aid:
                    return aid
        lid = loc.get("id") or ""
        if "arXiv.org:" in lid:
            aid = normalize_arxiv_id(lid.rsplit(":", 1)[-1])
            if aid:
                return aid
    return None


def parse_work(w: dict[str, Any], *, include_refs: bool = False) -> WorkRecord:
    ids = w.get("ids") or {}
    doi = normalize_doi(w.get("doi") or ids.get("doi"))
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    issns = []
    for i in [src.get("issn_l"), *(src.get("issn") or [])]:
        n = normalize_issn(i)
        if n and n not in issns:
            issns.append(n)
    isbns = [n for n in (normalize_isbn(i) for i in (src.get("isbn") or [])) if n]
    otype = w.get("type") or ""
    ptype = TYPE_MAP.get(otype, PublicationType.UNKNOWN)
    src_type = src.get("type") or ""
    if ptype == PublicationType.JOURNAL_ARTICLE and src_type == "conference":
        ptype = PublicationType.CONFERENCE_PAPER
    if ptype == PublicationType.JOURNAL_ARTICLE and src_type == "repository":
        ptype = PublicationType.PREPRINT
    arxiv = normalize_arxiv_id(doi) if doi and is_preprint_doi(doi) else None
    arxiv = arxiv or _arxiv_from_locations(w)
    pmid = ids.get("pmid")
    if pmid:
        pmid = pmid.rstrip("/").rsplit("/", 1)[-1]
    rec = WorkRecord(
        ids=ExternalIds(doi=doi, arxiv=arxiv, pmid=pmid, openalex=short_id(w.get("id"))),
        title=w.get("title") or w.get("display_name"),
        authors=[
            (a.get("author") or {}).get("display_name")
            for a in w.get("authorships") or []
            if (a.get("author") or {}).get("display_name")
        ],
        year=w.get("publication_year"),
        venue=src.get("display_name"),
        issns=issns,
        isbns=isbns,
        publisher=src.get("host_organization_name"),
        publication_type=ptype,
        raw_types={"openalex": f"{otype}/{src_type}" if src_type else otype},
        sources=["openalex"],
        extra={"openalex_biblio": w.get("biblio")},
    )
    if include_refs and w.get("referenced_works") is not None:
        rec.referenced_openalex_ids = [short_id(x) for x in w["referenced_works"] if x]
    return rec


class OpenAlexClient:
    name = "openalex"

    def __init__(self, settings: Settings, db: Database, **http_kwargs: Any):
        self.settings = settings
        self.http = HttpClient(self.name, settings, db, min_interval=0.12, **http_kwargs)
        self.cost_usd = 0.0

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(extra or {})
        if self.settings.openalex_api_key:
            params["api_key"] = self.settings.openalex_api_key
        if self.settings.openalex_mailto or self.settings.crossref_mailto:
            params["mailto"] = self.settings.openalex_mailto or self.settings.crossref_mailto
        return params

    def _track_cost(self, payload: dict[str, Any] | None) -> None:
        meta = (payload or {}).get("meta") or {}
        if isinstance(meta.get("cost_usd"), int | float):
            self.cost_usd += meta["cost_usd"]

    def get_work(self, identifier: str) -> WorkRecord | None:
        """``identifier``: ``W123``, ``doi:10.x/y``, or a bare DOI."""
        ident = identifier
        doi = normalize_doi(identifier)
        if doi:
            ident = f"doi:{doi}"
        resp = self.http.get(
            f"{BASE}/works/{ident}",
            self._params({"select": SELECT + ",referenced_works"}),
            ttl=self.settings.ttl.metadata,
        )
        if resp.status != 200:
            return None
        return parse_work(resp.json(), include_refs=True)

    def search_title(
        self, title: str, per_page: int = 5, extra_filter: str | None = None
    ) -> list[WorkRecord]:
        # Strip characters that are OpenAlex filter syntax.
        clean = title.replace(",", " ").replace(":", " ").replace("|", " ")
        flt = f"title.search:{clean}"
        if extra_filter:
            flt += "," + extra_filter
        resp = self.http.get(
            f"{BASE}/works",
            self._params({"filter": flt, "per_page": per_page, "select": SELECT}),
            ttl=self.settings.ttl.search,
        )
        if resp.status != 200:
            return []
        payload = resp.json()
        self._track_cost(payload)
        return [parse_work(w) for w in payload.get("results") or []]

    def citing_works(
        self, work_id: str, per_page: int = 100, max_pages: int = 50
    ) -> list[WorkRecord]:
        out: list[WorkRecord] = []
        cursor = "*"
        for _ in range(max_pages):
            resp = self.http.get(
                f"{BASE}/works",
                self._params(
                    {
                        "filter": f"cites:{work_id}",
                        "per_page": per_page,
                        "cursor": cursor,
                        "select": SELECT + ",referenced_works",
                    }
                ),
                ttl=self.settings.ttl.citations,
            )
            if resp.status != 200:
                break
            payload = resp.json() or {}
            self._track_cost(payload)
            for w in payload.get("results") or []:
                rec = parse_work(w, include_refs=True)
                rec.discovered_against.append(f"openalex:{work_id}")
                out.append(rec)
            cursor = (payload.get("meta") or {}).get("next_cursor")
            if not cursor or not payload.get("results"):
                break
        return out

"""Optional Elsevier Scopus API adapter (Scopus Search API only).

Used only when ``ELSEVIER_API_KEY`` is configured. Every failure is mapped to an explicit
status; missing permissions are *never* reported as "not indexed".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..db import Database
from ..matching import normalize_doi, normalize_issn
from ..models import (
    ExternalIds,
    PublicationType,
    ScopusArticleCheck,
    ScopusArticleStatus,
    WorkRecord,
)
from .base import HttpClient, PermissionDenied, ProviderError, RateLimited

SEARCH_URL = "https://api.elsevier.com/content/search/scopus"
# A long-established, certainly-indexed article (LeCun et al., Nature 2015) for `doctor`.
PROBE_DOI = "10.1038/nature14539"

SUBTYPE_MAP = {
    "ar": PublicationType.JOURNAL_ARTICLE,
    "re": PublicationType.JOURNAL_ARTICLE,
    "le": PublicationType.JOURNAL_ARTICLE,
    "ed": PublicationType.JOURNAL_ARTICLE,
    "no": PublicationType.JOURNAL_ARTICLE,
    "sh": PublicationType.JOURNAL_ARTICLE,
    "cp": PublicationType.CONFERENCE_PAPER,
    "ch": PublicationType.BOOK_CHAPTER,
    "bk": PublicationType.BOOK,
}


ABSTRACT_URL = "https://api.elsevier.com/content/abstract/eid/{eid}"


@dataclass
class ScopusReference:
    """One bibliography entry of a Scopus record, with the record Scopus linked it to."""

    position: str | None
    eid: str | None  # Scopus record the reference is linked to
    ref_type: str | None  # "resolvedReference" (indexed doc) or "originalReference/other"
    title: str | None
    sourcetitle: str | None
    doi: str | None
    year: int | None
    authors: list[str] = field(default_factory=list)


def _text(value: Any) -> str | None:
    """Scopus JSON sometimes gives a string, a list of strings or {"$": ...}."""
    if isinstance(value, list):
        value = next((v for v in value if v), None)
    if isinstance(value, dict):
        value = value.get("$")
    return str(value) if value else None


def _parse_reference(ref: dict[str, Any]) -> ScopusReference:
    authors = (ref.get("author-list") or {}).get("author") or []
    if isinstance(authors, dict):
        authors = [authors]
    year = None
    cover = str(ref.get("prism:coverDate") or "")
    if cover[:4].isdigit():
        year = int(cover[:4])
    return ScopusReference(
        position=ref.get("@id"),
        eid=ref.get("scopus-eid"),
        ref_type=ref.get("type"),
        title=_text(ref.get("title")),
        sourcetitle=_text(ref.get("sourcetitle")),
        doi=normalize_doi(_text(ref.get("ce:doi"))),
        year=year,
        authors=[
            a.get("ce:surname") for a in authors if isinstance(a, dict) and a.get("ce:surname")
        ],
    )


@dataclass
class CitingSet:
    """Documents Scopus associates as citing a given EID."""

    ok: bool
    eids: set[str] = field(default_factory=set)
    dois: set[str] = field(default_factory=set)
    records: list[WorkRecord] = field(default_factory=list)
    status: ScopusArticleStatus = ScopusArticleStatus.UNKNOWN
    detail: str = ""


def scopus_record_url(eid: str | None, entry: dict[str, Any] | None = None) -> str | None:
    """Scopus record page: the ``scopus`` link returned by the API, else built from the EID."""
    for link in (entry or {}).get("link") or []:
        if link.get("@ref") == "scopus" and link.get("@href"):
            return link["@href"]
    if eid:
        return f"https://www.scopus.com/record/display.uri?eid={eid}&origin=resultslist"
    return None


def _entry_to_record(e: dict[str, Any]) -> WorkRecord:
    year = None
    if (e.get("prism:coverDate") or "")[:4].isdigit():
        year = int(e["prism:coverDate"][:4])
    issns = [
        i for i in (normalize_issn(e.get("prism:issn")), normalize_issn(e.get("prism:eIssn"))) if i
    ]
    return WorkRecord(
        ids=ExternalIds(
            doi=normalize_doi(e.get("prism:doi")), scopus_eid=e.get("eid"), pmid=e.get("pubmed-id")
        ),
        title=e.get("dc:title"),
        authors=[e["dc:creator"]] if e.get("dc:creator") else [],
        year=year,
        venue=e.get("prism:publicationName"),
        issns=issns,
        publication_type=SUBTYPE_MAP.get(e.get("subtype", ""), PublicationType.UNKNOWN),
        raw_types={"scopus": e.get("subtypeDescription") or e.get("subtype") or ""},
        sources=["scopus"],
        extra={"scopus_url": scopus_record_url(e.get("eid"), e)},
    )


def _entries(payload: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    sr = payload.get("search-results") or {}
    total = int(sr.get("opensearch:totalResults") or 0)
    entries = [e for e in sr.get("entry") or [] if not e.get("error")]
    return total, entries


class ScopusClient:
    name = "scopus"

    def __init__(self, settings: Settings, db: Database, **http_kwargs: Any):
        self.settings = settings
        self.enabled = settings.scopus_api_configured
        headers = {"Accept": "application/json"}
        if settings.elsevier_api_key:
            headers["X-ELS-APIKey"] = settings.elsevier_api_key
        if settings.elsevier_insttoken:
            headers["X-ELS-Insttoken"] = settings.elsevier_insttoken
        http_kwargs.setdefault("max_retries", 3)
        self.http = HttpClient(
            self.name, settings, db, min_interval=0.2, headers=headers, **http_kwargs
        )
        # Once permission is denied we stop querying for the rest of the run.
        self.denied: str | None = None
        self.refs_denied: str | None = None  # view=REF needs institutional entitlement
        self.unavailable: str | None = None

    def _search(self, query: str, start: int = 0, count: int = 25) -> dict[str, Any]:
        resp = self.http.get(
            SEARCH_URL,
            {"query": query, "start": start, "count": count},
            ttl=self.settings.ttl.scopus_api,
        )
        if resp.status == 400:
            body = resp.body.decode("utf-8", "replace")
            if "not allowed for this requestor" in body:
                # The key works but this field (e.g. REFEID) is outside its entitlement.
                raise PermissionDenied(
                    self.name,
                    f"{query.split('(')[0]}() is not permitted for this API key "
                    "(typically needs the institutional network or ELSEVIER_INSTTOKEN)",
                    400,
                )
            raise ProviderError(self.name, f"bad query {query!r}: {body[:200]!r}", 400)
        if not resp.ok:
            raise ProviderError(self.name, f"HTTP {resp.status}", resp.status)
        return resp.json() or {}

    def _blocked_check(self) -> ScopusArticleCheck | None:
        if not self.enabled:
            return ScopusArticleCheck(
                status=ScopusArticleStatus.SCOPUS_API_UNAVAILABLE,
                detail="no ELSEVIER_API_KEY configured",
            )
        if self.denied:
            return ScopusArticleCheck(
                status=ScopusArticleStatus.SCOPUS_PERMISSION_DENIED, detail=self.denied
            )
        if self.unavailable:
            return ScopusArticleCheck(
                status=ScopusArticleStatus.SCOPUS_API_UNAVAILABLE, detail=self.unavailable
            )
        return None

    def lookup_doi(self, doi: str) -> ScopusArticleCheck:
        blocked = self._blocked_check()
        if blocked:
            return blocked
        doi_n = normalize_doi(doi)
        if not doi_n:
            return ScopusArticleCheck(status=ScopusArticleStatus.UNKNOWN, detail="no DOI")
        try:
            total, entries = _entries(self._search(f'DOI("{doi_n}")'))
        except PermissionDenied as exc:
            self.denied = f"HTTP {exc.status}: API key/insttoken lacks access ({exc})"
            return ScopusArticleCheck(
                status=ScopusArticleStatus.SCOPUS_PERMISSION_DENIED, detail=self.denied
            )
        except RateLimited as exc:
            self.unavailable = f"rate limited / quota exhausted: {exc}"
            return ScopusArticleCheck(
                status=ScopusArticleStatus.SCOPUS_API_UNAVAILABLE, detail=self.unavailable
            )
        except ProviderError as exc:
            if exc.status != 400:
                self.unavailable = str(exc)
            return ScopusArticleCheck(
                status=ScopusArticleStatus.SCOPUS_API_UNAVAILABLE, detail=str(exc)
            )
        if total == 0 or not entries:
            return ScopusArticleCheck(
                status=ScopusArticleStatus.SCOPUS_NOT_FOUND,
                detail=f"Scopus Search DOI({doi_n}) returned 0 results",
            )
        e = entries[0]
        return ScopusArticleCheck(
            status=ScopusArticleStatus.SCOPUS_CONFIRMED,
            eid=e.get("eid"),
            url=scopus_record_url(e.get("eid"), e),
            citedby_count=int(e["citedby-count"])
            if str(e.get("citedby-count", "")).isdigit()
            else None,
            citedby_url=next(
                (
                    lnk["@href"]
                    for lnk in e.get("link") or []
                    if lnk.get("@ref") == "scopus-citedby"
                ),
                None,
            ),
            source_title=e.get("prism:publicationName"),
            detail=f"Scopus Search DOI({doi_n}) -> {e.get('eid')}",
        )

    def citing_documents(self, eid: str, max_results: int = 2000) -> CitingSet:
        """Documents whose Scopus reference list is linked to ``eid`` (``REFEID``)."""
        blocked = self._blocked_check()
        if blocked:
            return CitingSet(ok=False, status=blocked.status, detail=blocked.detail)
        result = CitingSet(ok=True, status=ScopusArticleStatus.SCOPUS_CONFIRMED)
        start = 0
        try:
            while start < max_results:
                total, entries = _entries(self._search(f"REFEID({eid})", start=start))
                for e in entries:
                    rec = _entry_to_record(e)
                    if rec.ids.scopus_eid:
                        result.eids.add(rec.ids.scopus_eid)
                    if rec.ids.doi:
                        result.dois.add(rec.ids.doi)
                    rec.discovered_against.append(f"scopus:{eid}")
                    result.records.append(rec)
                start += len(entries)
                if not entries or start >= total:
                    break
        except PermissionDenied as exc:
            if exc.status != 400:  # 401/403: the whole key is unusable
                self.denied = f"HTTP {exc.status}: {exc}"
            return CitingSet(
                ok=False,
                status=ScopusArticleStatus.SCOPUS_PERMISSION_DENIED,
                detail=f"cited-by comparison unavailable: {exc}",
            )
        except ProviderError as exc:
            return CitingSet(
                ok=False, status=ScopusArticleStatus.SCOPUS_API_UNAVAILABLE, detail=str(exc)
            )
        result.detail = f"Scopus REFEID({eid}) returned {len(result.records)} citing documents"
        return result

    def references(self, eid: str) -> tuple[list[ScopusReference] | None, str]:
        """Bibliography of ``eid`` as Scopus sees it (Abstract Retrieval ``view=REF``).

        Returns ``(None, reason)`` when unavailable. A denial here does not disable the
        Search API, which the same key may still use.
        """
        if not self.enabled:
            return None, "no ELSEVIER_API_KEY configured"
        if self.denied or self.refs_denied:
            return None, self.denied or self.refs_denied or ""
        refs: list[ScopusReference] = []
        params: dict[str, Any] = {"view": "REF"}
        while True:
            try:
                resp = self.http.get(
                    ABSTRACT_URL.format(eid=eid), params, ttl=self.settings.ttl.scopus_api
                )
            except PermissionDenied as exc:
                self.refs_denied = (
                    f"Scopus reference lists (view=REF) not permitted: HTTP {exc.status}; "
                    "usually needs the institutional network/VPN or ELSEVIER_INSTTOKEN"
                )
                return None, self.refs_denied
            except ProviderError as exc:
                return None, str(exc)
            if not resp.ok:
                return None, f"Scopus reference list of {eid}: HTTP {resp.status}"
            data = (resp.json() or {}).get("abstracts-retrieval-response") or {}
            block = data.get("references") or {}
            items = block.get("reference") or []
            if isinstance(items, dict):
                items = [items]
            refs += [_parse_reference(r) for r in items]
            total = int(block.get("@total-references") or 0)
            if not items or len(refs) >= total:
                break
            # Only ``startref``: Scopus rejects some valid-looking ``refcount`` values on the
            # last page, while ``startref`` alone always returns the next (up to 40) entries.
            params = {"view": "REF", "startref": len(refs) + 1}
        return refs, "ok"

    def capabilities(self) -> list[tuple[str, bool, str]]:
        """Test each Scopus feature the tool uses with the current key/network (for doctor)."""
        out: list[tuple[str, bool, str]] = []

        def get(url: str, params: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
            try:
                r = self.http.get(url, params, ttl=0, use_cache=False)
            except ProviderError as exc:
                return exc.status or 0, {}, str(exc)
            body = r.body.decode("utf-8", "replace")
            return r.status, (r.json() if r.ok else {}), body[:160]

        status, data, err = get(SEARCH_URL, {"query": f'DOI("{PROBE_DOI}")'})
        entries = _entries(data)[1] if data else []
        eid = entries[0].get("eid") if entries else None
        out.append(
            (
                "Search by DOI (article indexed?)",
                bool(eid),
                f"-> {eid}" if eid else f"HTTP {status} {err}",
            )
        )
        count = entries[0].get("citedby-count") if entries else None
        out.append(
            (
                "Cited-by count",
                count is not None,
                f"{count} citations" if count is not None else "field not returned",
            )
        )
        if not eid:
            return out
        status, data, err = get(SEARCH_URL, {"query": f"REFEID({eid})", "count": 1})
        n = (data.get("search-results") or {}).get("opensearch:totalResults") if data else None
        out.append(
            (
                "Cited-by list, REFEID() (per-article linkage check)",
                n is not None,
                f"{n} citing documents" if n is not None else f"HTTP {status} {err}",
            )
        )
        status, data, err = get(
            f"https://api.elsevier.com/content/abstract/eid/{eid}", {"view": "REF"}
        )
        out.append(
            (
                "Abstract Retrieval view=REF (reference linking evidence)",
                status == 200,
                "references available" if status == 200 else f"HTTP {status} {err}",
            )
        )
        return out

    def probe(self) -> tuple[ScopusArticleStatus, str]:
        """Lightweight credential check used by ``doctor``."""
        check = self.lookup_doi(PROBE_DOI)
        if check.status in (
            ScopusArticleStatus.SCOPUS_CONFIRMED,
            ScopusArticleStatus.SCOPUS_NOT_FOUND,
        ):
            return check.status, "Scopus Search API reachable with current credentials"
        return check.status, check.detail

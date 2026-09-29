"""Sync orchestration: resolve versions → discover → dedupe → enrich → classify → reconcile."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings
from .db import Database
from .matching import (
    Cluster,
    EntityResolver,
    arxiv_doi,
    confident_title_match,
    first_author_matches,
    is_preprint_doi,
    merge_records,
    normalize_arxiv_id,
    normalize_doi,
    title_similarity,
)
from .models import (
    ExternalIds,
    LinkageStatus,
    PublicationType,
    ScopusArticleCheck,
    ScopusArticleStatus,
    ScopusSourceStatus,
    TargetVersion,
    TrackedPaper,
    WorkRecord,
)
from .providers.base import ProviderError
from .providers.crossref import CrossrefClient
from .providers.openalex import OpenAlexClient
from .providers.scholar_import import split_authors
from .providers.scopus import CitingSet, ScopusClient, scopus_record_url
from .providers.scopus_sources import SourceIndex
from .providers.semantic_scholar import SemanticScholarClient
from .reconciliation import (
    COVERED,
    CountAnalysis,
    analyse_counts,
    apply_manual_target,
    classify_target,
    reconcile,
    scopus_reference_link,
)

log = logging.getLogger(__name__)

SCHOLAR = "google_scholar_import"
MANUAL = "manual"


@dataclass
class Providers:
    crossref: CrossrefClient
    s2: SemanticScholarClient
    openalex: OpenAlexClient
    scopus: ScopusClient

    @classmethod
    def build(
        cls,
        settings: Settings,
        db: Database,
        transport: httpx.BaseTransport | None = None,
        **kw: Any,
    ) -> Providers:
        return cls(
            crossref=CrossrefClient(settings, db, transport=transport, **kw),
            s2=SemanticScholarClient(settings, db, transport=transport, **kw),
            openalex=OpenAlexClient(settings, db, transport=transport, **kw),
            scopus=ScopusClient(settings, db, transport=transport, **kw),
        )

    def all(self) -> list[Any]:
        return [self.crossref, self.s2, self.openalex, self.scopus]

    def close(self) -> None:
        for p in self.all():
            p.http.close()


@dataclass
class SyncReport:
    paper: str
    stats: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    status: str = "ok"


class Syncer:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        providers: Providers,
        progress: Callable[[str], None] | None = None,
    ):
        self.settings = settings
        self.db = db
        self.p = providers
        self.progress = progress or (lambda msg: None)
        self.warnings: list[str] = []
        self.failed_discovery: set[str] = set()
        self.vor_scopus: ScopusArticleCheck | None = None

    # ------------------------------------------------------------------ helpers
    def _call(
        self, label: str, fn: Callable[..., Any], *args: Any, discovery: bool = False, **kwargs: Any
    ) -> Any:
        try:
            return fn(*args, **kwargs)
        except ProviderError as exc:
            msg = f"{label}: {type(exc).__name__}: {exc}"
            if msg not in self.warnings:
                self.warnings.append(msg)
            log.info(msg)
            if discovery:
                self.failed_discovery.add(exc.provider)
            return None

    # ------------------------------------------------------------------ step 1
    def resolve_versions(self, paper: TrackedPaper) -> TrackedPaper:
        self.progress(f"[{paper.name}] resolving preprint and Version of Record")
        paper.arxiv_id = normalize_arxiv_id(paper.arxiv_id) or paper.arxiv_id
        paper.journal_doi = normalize_doi(paper.journal_doi)
        if paper.arxiv_id and not paper.arxiv_doi:
            paper.arxiv_doi = arxiv_doi(paper.arxiv_id)
        paper.arxiv_doi = normalize_doi(paper.arxiv_doi)
        previous = paper.version_ids  # from the last successful sync, if any
        versions: dict[str, dict[str, set[str]]] = {"preprint": {}, "vor": {}}
        rows: list[tuple[str, str, str, str | None, str | None]] = []

        def add(
            version: str,
            provider: str,
            pid: str | None,
            title: str | None = None,
            detail: str | None = None,
        ) -> None:
            if pid:
                versions[version].setdefault(provider, set()).add(pid)
                rows.append((version, provider, pid, title, detail))

        # Crossref: authoritative VoR metadata.
        if paper.journal_doi:
            vor = self._call("crossref VoR lookup", self.p.crossref.get_work, paper.journal_doi)
            if vor:
                paper.title = (
                    paper.title
                    if paper.title and len(paper.title) > len(vor.title or "")
                    else (vor.title or paper.title)
                )
                paper.authors = paper.authors or vor.authors
                paper.journal_title = vor.venue or paper.journal_title
                paper.journal_issns = vor.issns or paper.journal_issns
                paper.volume = vor.extra.get("crossref_volume") or paper.volume
                paper.pages = vor.extra.get("crossref_page") or paper.pages
                paper.year = vor.year or paper.year
                add("vor", "crossref", paper.journal_doi, vor.title)
                rel = vor.extra.get("crossref_relation") or {}
                for d in rel.get("has-preprint", []):
                    aid = normalize_arxiv_id(d or "")
                    if aid and not paper.arxiv_id:
                        paper.arxiv_id, paper.arxiv_doi = aid, arxiv_doi(aid)
                        self.warnings.append(f"arXiv id {aid} taken from Crossref has-preprint")
            elif vor is None:
                self.warnings.append(f"final DOI {paper.journal_doi} not found in Crossref")

        # Semantic Scholar: usually one merged record for both versions.
        s2_ids: set[str] = set()
        merged_arxiv: str | None = None
        for label, ident, version in (
            ("DOI", paper.journal_doi, "vor"),
            ("ARXIV", paper.arxiv_id, "preprint"),
        ):
            if not ident:
                continue
            if label == "ARXIV" and merged_arxiv == ident:
                # The DOI lookup already returned S2's merged record for both versions.
                add(
                    "preprint",
                    "semantic_scholar",
                    next(iter(s2_ids)),
                    None,
                    "Semantic Scholar merges the preprint into the journal record",
                )
                continue
            rec = self._call(
                f"semantic scholar {label} lookup",
                self.p.s2.get_paper,
                f"{label}:{ident}",
                discovery=True,
            )
            if rec and rec.ids.s2:
                s2_ids.add(rec.ids.s2)
                if label == "DOI":
                    merged_arxiv = rec.ids.arxiv
                add(version, "semantic_scholar", rec.ids.s2, rec.title)
                paper.title = paper.title or rec.title
                paper.authors = paper.authors or rec.authors
                if not paper.journal_doi and rec.ids.doi and not is_preprint_doi(rec.ids.doi):
                    paper.journal_doi = rec.ids.doi
                    self.warnings.append(
                        f"final DOI {rec.ids.doi} inferred from Semantic "
                        "Scholar (verify with `paper add --doi`)"
                    )
                if not paper.arxiv_id and rec.ids.arxiv:
                    paper.arxiv_id, paper.arxiv_doi = rec.ids.arxiv, arxiv_doi(rec.ids.arxiv)

        # OpenAlex: versions may be separate works, merged, or one of them missing.
        oa_vor = (
            self._call(
                "openalex VoR lookup", self.p.openalex.get_work, paper.journal_doi, discovery=True
            )
            if paper.journal_doi
            else None
        )
        oa_pre = (
            self._call(
                "openalex preprint lookup",
                self.p.openalex.get_work,
                paper.arxiv_doi,
                discovery=True,
            )
            if paper.arxiv_doi
            else None
        )
        if oa_vor:
            add("vor", "openalex", oa_vor.ids.openalex, oa_vor.title)
            if oa_vor.ids.arxiv and paper.arxiv_id and oa_vor.ids.arxiv == paper.arxiv_id:
                add(
                    "preprint",
                    "openalex",
                    oa_vor.ids.openalex,
                    oa_vor.title,
                    "OpenAlex merged the preprint into the journal record",
                )
        if oa_pre:
            add("preprint", "openalex", oa_pre.ids.openalex, oa_pre.title)
        if paper.title and (not oa_pre or not oa_vor):
            for cand in (
                self._call("openalex title search", self.p.openalex.search_title, paper.title) or []
            ):
                if title_similarity(cand.title, paper.title) < 90:
                    continue
                if (
                    paper.authors
                    and cand.authors
                    and not first_author_matches(paper.authors, cand.authors)
                ):
                    continue
                is_pre = (
                    cand.publication_type == PublicationType.PREPRINT
                    or (cand.ids.doi and is_preprint_doi(cand.ids.doi))
                    or (paper.arxiv_id and cand.ids.arxiv == paper.arxiv_id)
                )
                if is_pre and not oa_pre:
                    add(
                        "preprint",
                        "openalex",
                        cand.ids.openalex,
                        cand.title,
                        "found by title search",
                    )
                elif (
                    not is_pre
                    and not oa_vor
                    and (not paper.journal_doi or cand.ids.doi in (None, paper.journal_doi))
                ):
                    add("vor", "openalex", cand.ids.openalex, cand.title, "found by title search")

        # Optional Scopus: EID of the Version of Record.
        if self.p.scopus.enabled and paper.journal_doi:
            chk = self.p.scopus.lookup_doi(paper.journal_doi)
            if chk.status == ScopusArticleStatus.SCOPUS_CONFIRMED and chk.eid:
                add("vor", "scopus", chk.eid, chk.source_title, chk.url)
                self.vor_scopus = chk
            else:
                self.warnings.append(f"Scopus VoR lookup: {chk.status.value} {chk.detail}")

        # A provider that failed now keeps the record ids found by earlier syncs.
        for version, by_provider in previous.items():
            for provider, joined in by_provider.items():
                if provider in self.failed_discovery and provider not in versions[version]:
                    for pid in joined.split("|"):
                        add(version, provider, pid, None, "kept from previous sync")

        assert paper.id is not None
        self.db.upsert_paper(paper)
        self.db.set_paper_versions(paper.id, rows)
        paper.version_ids = {
            v: {k: "|".join(sorted(ids)) for k, ids in d.items()} for v, d in versions.items()
        }
        return paper

    # ------------------------------------------------------------------ step 2
    def discover(self, paper: TrackedPaper) -> tuple[list[WorkRecord], CitingSet | None]:
        records: list[WorkRecord] = []
        s2_ids = {
            i
            for v in paper.version_ids.values()
            for i in v.get("semantic_scholar", "").split("|")
            if i
        }
        for sid in sorted(s2_ids):
            self.progress(f"[{paper.name}] Semantic Scholar citations of {sid}")
            got = self._call("semantic scholar citations", self.p.s2.citations, sid, discovery=True)
            records += got or []
        oa_ids = {
            i for v in paper.version_ids.values() for i in v.get("openalex", "").split("|") if i
        }
        for wid in sorted(oa_ids):
            self.progress(f"[{paper.name}] OpenAlex works citing {wid}")
            got = self._call("openalex cites", self.p.openalex.citing_works, wid, discovery=True)
            records += got or []
        vor_citers: CitingSet | None = None
        eid = paper.version_ids.get("vor", {}).get("scopus")
        if eid and self.p.scopus.enabled:
            self.progress(f"[{paper.name}] Scopus documents citing {eid}")
            vor_citers = self.p.scopus.citing_documents(eid.split("|")[0])
            if vor_citers.ok:
                records += vor_citers.records
            else:
                self.warnings.append(
                    f"Scopus cited-by: {vor_citers.status.value} {vor_citers.detail}"
                )
        assert paper.id is not None
        scholar = self.db.scholar_rows(paper.id)
        if scholar:
            self.progress(f"[{paper.name}] resolving {len(scholar)} Google Scholar rows")
        for row in scholar:
            records.append(self.resolve_scholar_row(row))
        for doi in self.db.manual_citations(paper.id):
            records.append(
                WorkRecord(
                    ids=ExternalIds(doi=doi),
                    sources=[MANUAL],
                    discovered_against=[f"{MANUAL}:user"],
                )
            )
        return records, vor_citers

    def resolve_scholar_row(self, row: dict[str, Any]) -> WorkRecord:
        authors = split_authors(row.get("authors"))
        rec = WorkRecord(
            ids=ExternalIds(doi=row.get("doi"), arxiv=row.get("arxiv")),
            title=row.get("title"),
            authors=authors,
            year=row.get("year"),
            venue=row.get("journal"),
            sources=[SCHOLAR],
            discovered_against=[f"{SCHOLAR}:manual"],
            extra={"scholar_row": row},
        )
        if rec.ids.doi or not rec.title:
            return rec

        def acceptable(cand: WorkRecord | None, score: float | None = None) -> bool:
            return confident_title_match(rec, cand, score)

        resolved: WorkRecord | None = None
        how = ""
        got = self._call(
            "crossref title resolution", self.p.crossref.resolve_title, rec.title, authors, rec.year
        )
        if got and acceptable(got[0], got[1]):
            resolved, how = got[0], f"crossref bibliographic match ({got[1]:.0f})"
        if not resolved:
            cand = self._call("semantic scholar title match", self.p.s2.match_title, rec.title)
            if acceptable(cand):
                resolved, how = cand, "semantic scholar title match"
        if not resolved:
            for cand in (
                self._call("openalex title search", self.p.openalex.search_title, rec.title) or []
            ):
                if acceptable(cand):
                    resolved, how = cand, "openalex title search"
                    break
        if resolved:
            rec.ids.merge(resolved.ids)
            rec.extra["scholar_resolution"] = how
            rec.issns = rec.issns or resolved.issns
            rec.publication_type = resolved.publication_type
        else:
            rec.extra["scholar_resolution"] = "unresolved (no confident DOI match)"
        return rec

    # ------------------------------------------------------------------ step 3
    def resolve_missing_doi(self, rec: WorkRecord) -> None:
        """Try to find a DOI for a citing work known only by title (strict thresholds)."""
        if rec.ids.doi or not rec.title or rec.ids.arxiv:
            return
        got = self._call(
            "crossref title resolution",
            self.p.crossref.resolve_title,
            rec.title,
            rec.authors,
            rec.year,
        )
        if got and got[0] and got[0].ids.doi and confident_title_match(rec, got[0], got[1]):
            rec.ids.doi = got[0].ids.doi
            rec.extra["doi_resolution"] = f"crossref bibliographic match ({got[1]:.0f})"

    def enrich(self, cl: Cluster) -> None:
        rec = cl.record
        doi = rec.ids.doi
        if doi and not is_preprint_doi(doi) and "crossref" not in rec.sources:
            cr = self._call("crossref enrichment", self.p.crossref.get_work, doi)
            if cr:
                cl.warnings += merge_records(rec, cr)
                if cr.title:
                    rec.title = cr.title
                # Crossref's type is authoritative, except that conference proceedings
                # deposited as book chapters (e.g. LNCS) stay conference papers.
                chapter_of_proceedings = (
                    cr.publication_type == PublicationType.BOOK_CHAPTER
                    and rec.publication_type == PublicationType.CONFERENCE_PAPER
                )
                if cr.publication_type != PublicationType.UNKNOWN and not chapter_of_proceedings:
                    rec.publication_type = cr.publication_type
        if doi and not rec.issns and "openalex" not in rec.sources:
            oa = self._call("openalex enrichment", self.p.openalex.get_work, doi)
            if oa:
                cl.warnings += merge_records(rec, oa)
        # A citing work known only through its arXiv DOI is a preprint.
        if (
            rec.ids.doi
            and is_preprint_doi(rec.ids.doi)
            and rec.publication_type in (PublicationType.UNKNOWN, PublicationType.JOURNAL_ARTICLE)
            and not rec.issns
        ):
            rec.publication_type = PublicationType.PREPRINT
        if not rec.ids.doi and rec.ids.arxiv and rec.publication_type == PublicationType.UNKNOWN:
            rec.publication_type = PublicationType.PREPRINT

    # ------------------------------------------------------------------ main
    def sync(self, paper: TrackedPaper) -> SyncReport:
        assert paper.id is not None
        run_id = self.db.start_run(paper.id)
        self.warnings = []
        self.failed_discovery = set()
        try:
            report = self._sync(paper)
        except Exception as exc:
            self.db.finish_run(run_id, "error", {}, [*self.warnings, repr(exc)])
            raise
        self.db.finish_run(run_id, report.status, report.stats, report.warnings)
        return report

    def _is_self(self, paper: TrackedPaper, rec: WorkRecord) -> bool:
        ids = rec.ids
        return bool(
            (ids.doi and ids.doi in (paper.journal_doi, paper.arxiv_doi))
            or (ids.arxiv and paper.arxiv_id and ids.arxiv == paper.arxiv_id)
            or (
                paper.version_ids
                and ids.s2
                and ids.s2
                in paper.version_ids.get("vor", {}).get("semantic_scholar", "").split("|")
            )
        )

    def _sync(self, paper: TrackedPaper) -> SyncReport:
        paper = self.resolve_versions(paper)
        raw, vor_citers = self.discover(paper)
        resolver = EntityResolver()
        for rec in raw:
            if not self._is_self(paper, rec):
                resolver.add(rec)
        for cl in resolver.clusters:
            self.resolve_missing_doi(cl.record)
        resolver.merge_by_doi()
        clusters = resolver.clusters
        self.progress(
            f"[{paper.name}] {len(raw)} raw citation records → {len(clusters)} "
            "unique citing works; enriching"
        )
        index = SourceIndex(self.db)
        if not index.loaded:
            self.warnings.append(
                "Scopus Source List not loaded: run `citation-reconciler scopus-sources update`"
            )
        manual_rows = self.db.manual_citations(paper.id)
        keep: set[int] = set()
        counts: dict[str, int] = {}
        assert paper.id is not None
        pending = []
        for i, cl in enumerate(clusters, 1):
            if i % 10 == 0:
                self.progress(f"[{paper.name}] enriched {i}/{len(clusters)}")
            self.enrich(cl)
            rec = cl.record
            if self._is_self(paper, rec):
                continue
            src = index.match(rec)
            art, linkage = self._scopus_article(rec, src.status, vor_citers)
            if art.eid:
                rec.ids.scopus_eid = art.eid
            if art.url:
                rec.extra["scopus_url"] = art.url
            ref_evidence: list[str] = []
            if (
                art.status == ScopusArticleStatus.SCOPUS_CONFIRMED
                and art.eid
                and (linkage == LinkageStatus.UNVERIFIED)
            ):
                refs, why = self.p.scopus.references(art.eid)
                vor_eid = (paper.version_ids.get("vor", {}).get("scopus") or "").split("|")[0]
                link = scopus_reference_link(paper, refs, vor_eid or None)
                if link is None:
                    if why not in self.warnings and why != "ok":
                        self.warnings.append(why)
                else:
                    linkage = link.status
                    ref_evidence = link.evidence
                    if link.reference is not None:
                        # Scopus' own reference also informs which version is cited.
                        rec.references = [*(rec.references or []), link.reference]
                    if link.linked_eid:
                        rec.extra["scopus_linked_eid"] = link.linked_eid
            target = classify_target(paper, rec)
            manual = manual_rows.get(rec.ids.doi or "")
            if manual:
                target = apply_manual_target(target, manual)
            pending.append((cl, rec, target, src, art, linkage, ref_evidence))

        counts_check = self._count_check(paper, pending, vor_citers)
        for cl, rec, target, src, art, linkage, ref_evidence in pending:
            linkage_evidence: list[str] = list(ref_evidence)
            if counts_check and art.status == ScopusArticleStatus.SCOPUS_CONFIRMED:
                linkage_evidence.append(counts_check.summary)
                deducible = linkage == LinkageStatus.UNVERIFIED and not (
                    "vor_explicit" in target.signals
                    or target.target_version == TargetVersion.VERSION_OF_RECORD
                )
                if counts_check.all_others_unlinked and deducible and not ref_evidence:
                    linkage = LinkageStatus.NOT_LINKED
                    linkage_evidence.append(
                        f"all {counts_check.citedby} counted citation(s) can be accounted for by "
                        "articles citing the final DOI explicitly, so this citation is not "
                        "counted for the Version of Record"
                    )
            discovered_via = sorted({s for m in cl.members for s in m.sources if s != "crossref"})
            result = reconcile(
                work=rec,
                discovered_via=discovered_via,
                target=target,
                src=src,
                art=art,
                linkage=linkage,
                identity_methods=cl.merge_methods,
                warnings=cl.warnings,
                review_reasons=cl.review_reasons,
                linkage_evidence=linkage_evidence,
            )
            work_id = self.db.upsert_work(rec)
            sources = sorted(
                {
                    (s, a)
                    for m in cl.members
                    for s in m.sources
                    if s != "crossref"
                    for a in (m.discovered_against or [""])
                }
            )
            self.db.upsert_citation(paper.id, work_id, target, result, sources)
            for reason in result.review_reasons:
                self.db.add_review(paper.id, work_id, reason)
            keep.add(work_id)
            counts[result.action.value] = counts.get(result.action.value, 0) + 1
        self.db.conn.commit()
        pruned = 0
        if not self.failed_discovery:
            pruned = self.db.prune_citations(paper.id, keep)
        else:
            self.warnings.append(
                "some discovery providers failed; previously found citations were kept"
            )
        by_source: dict[str, int] = {}
        for rec in raw:
            for s in rec.sources:
                by_source[s] = by_source.get(s, 0) + 1
        stats = {
            "raw_records": len(raw),
            "unique_works": len(keep),
            "by_source": by_source,
            "actions": counts,
            "pruned": pruned,
            "requests": {p.name: p.http.requests_made for p in self.p.all()},
            "cache_hits": {p.name: p.http.cache_hits for p in self.p.all()},
            "openalex_cost_usd": round(self.p.openalex.cost_usd, 5),
        }
        status = "partial" if self.failed_discovery else "ok"
        return SyncReport(
            paper=paper.name, stats=stats, warnings=list(self.warnings), status=status
        )

    def _count_check(
        self, paper: TrackedPaper, pending: list, vor_citers: CitingSet | None
    ) -> CountAnalysis | None:
        """Scopus cited-by count of the Version of Record vs. confirmed citing articles."""
        vor = self.vor_scopus
        assert paper.id is not None
        if vor is None or vor.citedby_count is None:
            self.db.set_meta(f"scopus_vor:{paper.id}", None)
            return None
        if vor_citers and vor_citers.ok:
            analysis = None  # exact per-article comparison is available; no deduction needed
        else:
            confirmed = [p for p in pending if p[4].status == ScopusArticleStatus.SCOPUS_CONFIRMED]
            explicit = sum(
                1
                for p in confirmed
                if "vor_explicit" in p[2].signals
                or (
                    p[2].target_version == TargetVersion.VERSION_OF_RECORD
                    and "manual_confirmed" in p[2].signals
                )
            )
            analysis = analyse_counts(vor.citedby_count, len(confirmed), explicit)
        self.db.set_meta(
            f"scopus_vor:{paper.id}",
            {
                "eid": vor.eid,
                "url": vor.url,
                "citedby_count": vor.citedby_count,
                "citedby_url": vor.citedby_url,
                "count_check": analysis.summary if analysis else None,
                "deficit": analysis.deficit if analysis else None,
                "confirmed_citing": analysis.confirmed if analysis else None,
            },
        )
        return analysis

    def _scopus_article(
        self, rec: WorkRecord, src_status: ScopusSourceStatus, vor_citers: CitingSet | None
    ) -> tuple[ScopusArticleCheck, LinkageStatus]:
        in_vor_citers = bool(
            vor_citers
            and vor_citers.ok
            and (
                (rec.ids.scopus_eid and rec.ids.scopus_eid in vor_citers.eids)
                or (rec.ids.doi and rec.ids.doi in vor_citers.dois)
            )
        )
        if not self.p.scopus.enabled:
            status = (
                ScopusArticleStatus.SCOPUS_SOURCE_ONLY
                if src_status in COVERED
                else ScopusArticleStatus.SCOPUS_API_UNAVAILABLE
            )
            detail = (
                "source is in the Scopus Source List; article-level indexing not "
                "verified (no Scopus API key)"
                if src_status in COVERED
                else "no Scopus API key configured"
            )
            return (ScopusArticleCheck(status=status, detail=detail), LinkageStatus.UNVERIFIED)
        if in_vor_citers:
            eid = next(
                (
                    r.ids.scopus_eid
                    for r in vor_citers.records  # type: ignore[union-attr]
                    if (rec.ids.doi and r.ids.doi == rec.ids.doi)
                    or (rec.ids.scopus_eid and r.ids.scopus_eid == rec.ids.scopus_eid)
                ),
                None,
            )
            return (
                ScopusArticleCheck(
                    status=ScopusArticleStatus.SCOPUS_CONFIRMED,
                    eid=eid,
                    url=scopus_record_url(eid),
                    detail="listed by Scopus among documents citing the Version of Record",
                ),
                LinkageStatus.LINKED_TO_VOR,
            )
        if rec.publication_type == PublicationType.PREPRINT or not rec.ids.doi:
            return (
                ScopusArticleCheck(
                    status=ScopusArticleStatus.UNKNOWN, detail="no DOI to look up in Scopus"
                ),
                LinkageStatus.NOT_APPLICABLE,
            )
        chk = self.p.scopus.lookup_doi(rec.ids.doi)
        if chk.status == ScopusArticleStatus.SCOPUS_CONFIRMED:
            if vor_citers and vor_citers.ok:
                return chk, LinkageStatus.NOT_LINKED
            return chk, LinkageStatus.UNVERIFIED
        if (
            chk.status
            in (
                ScopusArticleStatus.SCOPUS_PERMISSION_DENIED,
                ScopusArticleStatus.SCOPUS_API_UNAVAILABLE,
            )
            and src_status in COVERED
        ):
            chk.detail = f"{chk.detail}; source is in the Scopus Source List"
        return chk, (
            LinkageStatus.NOT_APPLICABLE
            if chk.status == ScopusArticleStatus.SCOPUS_NOT_FOUND
            else LinkageStatus.UNVERIFIED
        )

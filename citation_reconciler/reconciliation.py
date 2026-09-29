"""Target-version classification, recommended actions and explainable confidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .matching import (
    author_surname,
    find_arxiv_ids,
    find_dois,
    normalize_title,
    title_in_text,
    title_similarity,
)
from .models import (
    Action,
    Evidence,
    LinkageStatus,
    PublicationType,
    ReconciliationResult,
    ReferenceEntry,
    ScopusArticleCheck,
    ScopusArticleStatus,
    ScopusSourceStatus,
    SourceMatch,
    TargetClassification,
    TargetVersion,
    TrackedPaper,
    WorkRecord,
)

REFERENCE_TITLE_THRESHOLD = 90.0

COVERED = (ScopusSourceStatus.COVERED, ScopusSourceStatus.COVERED_TITLE_MATCH)
NOT_COVERED = (ScopusSourceStatus.NOT_IN_LIST, ScopusSourceStatus.OUTSIDE_COVERAGE)


def arxiv_year(arxiv_id: str | None) -> int | None:
    if arxiv_id and re.fullmatch(r"\d{4}\.\d{4,5}", arxiv_id):
        return 2000 + int(arxiv_id[:2])
    return None


# --------------------------------------------------------------------------- reference matching


@dataclass
class RefSignals:
    ref: ReferenceEntry
    about_paper: bool = False
    preprint: list[str] = field(default_factory=list)
    vor_publisher: list[str] = field(default_factory=list)  # strong VoR evidence
    vor_crossref: list[str] = field(default_factory=list)  # DOI attached by Crossref matching
    identity: list[str] = field(default_factory=list)


def analyse_reference(paper: TrackedPaper, ref: ReferenceEntry) -> RefSignals:
    s = RefSignals(ref=ref)
    text = ref.text()
    low = text.lower()
    dois_in_text = set(find_dois(text))
    arxiv_doi = paper.arxiv_doi
    vor_doi = paper.journal_doi

    # --- identity -------------------------------------------------------------
    if ref.doi and ref.doi in (arxiv_doi, vor_doi):
        s.identity.append(f"reference DOI {ref.doi}")
    if paper.arxiv_id and (
        paper.arxiv_id in find_arxiv_ids(text)
        or re.search(rf"(?<![\d.]){re.escape(paper.arxiv_id)}(?!\d)", text)
    ):
        s.identity.append(f"reference contains arXiv id {paper.arxiv_id}")
    if (vor_doi and vor_doi in dois_in_text) or (arxiv_doi and arxiv_doi in dois_in_text):
        s.identity.append("reference text contains a DOI of the paper")
    tscore = 0.0
    if paper.title:
        tscore = max(
            title_similarity(paper.title, ref.article_title), title_in_text(paper.title, text)
        )
        if tscore >= REFERENCE_TITLE_THRESHOLD:
            first = author_surname(paper.authors[0]) if paper.authors else ""
            if not first or first in normalize_title(text).replace(" ", "") or tscore >= 97:
                s.identity.append(f"reference title matches ({tscore:.0f})")
    if ref.matched:
        s.identity.append(f"entry of the {ref.source} reference list matched to the paper")
    s.about_paper = bool(s.identity)
    if not s.about_paper:
        return s

    # --- preprint signals --------------------------------------------------------
    if ref.doi and arxiv_doi and ref.doi == arxiv_doi:
        s.preprint.append(f"reference DOI is the arXiv DOI {arxiv_doi}")
    if arxiv_doi and arxiv_doi in dois_in_text:
        s.preprint.append(f"reference text contains arXiv DOI {arxiv_doi}")
    ids = find_arxiv_ids(text)
    if paper.arxiv_id and paper.arxiv_id in ids:
        s.preprint.append(f"reference contains arXiv:{paper.arxiv_id}")
    elif paper.arxiv_id and re.search(rf"(?<![\d.]){re.escape(paper.arxiv_id)}(?!\d)", text):
        s.preprint.append(f"reference contains the arXiv number {paper.arxiv_id}")
    if "arxiv.org" in low:
        s.preprint.append("reference contains an arxiv.org URL")
    venue_text = f"{ref.journal_title or ''} {ref.unstructured or ''}".lower()
    if re.search(r"\barxiv\b", venue_text) and not any("arXiv" in p for p in s.preprint):
        s.preprint.append("reference venue/text mentions arXiv")

    # --- version-of-record signals ----------------------------------------------
    if vor_doi and ref.doi == vor_doi:
        who = ref.doi_asserted_by or "unknown"
        msg = f"reference DOI is the final DOI {vor_doi} (doi-asserted-by: {who})"
        if who == "crossref" and vor_doi not in dois_in_text:
            s.vor_crossref.append(msg)
        else:
            s.vor_publisher.append(msg)
    if vor_doi and vor_doi in dois_in_text:
        s.vor_publisher.append(f"reference text contains final DOI {vor_doi}")
    if paper.journal_title:
        jt = normalize_title(paper.journal_title)
        in_journal = bool(jt) and (
            jt == normalize_title(ref.journal_title) or f" {jt} " in f" {normalize_title(text)} "
        )
        has_locator = bool(ref.volume or ref.first_page) or bool(
            paper.volume and re.search(rf"\b{re.escape(paper.volume)}\b", text)
        )
        if in_journal and has_locator:
            s.vor_publisher.append(
                f"reference names journal '{paper.journal_title}' with volume/pages"
            )
        elif in_journal:
            s.vor_crossref.append(
                f"reference names journal '{paper.journal_title}' (no volume/pages)"
            )
    return s


def classify_target(paper: TrackedPaper, work: WorkRecord) -> TargetClassification:
    """Decide whether ``work`` cites the preprint, the Version of Record, both, or unknown.

    Only bibliography metadata is used. Discovery through a preprint record in a citation
    graph is *never* taken as evidence (graphs merge versions).
    """
    out = TargetClassification()
    hints = []
    if work.referenced_openalex_ids is not None and paper.version_ids:
        pre = set(paper.version_ids.get("preprint", {}).get("openalex", "").split("|")) - {""}
        vor = set(paper.version_ids.get("vor", {}).get("openalex", "").split("|")) - {""}
        refs = set(work.referenced_openalex_ids)
        if pre and vor and pre != vor:
            if refs & pre and not refs & vor:
                hints.append("hint only: OpenAlex resolved the reference to the preprint record")
            elif refs & vor and not refs & pre:
                hints.append("hint only: OpenAlex resolved the reference to the journal record")

    if work.references is None:
        out.evidence = [
            "no reference list available (Crossref has no deposited references for this work)",
            *hints,
        ]
        return out
    if not work.references:
        out.evidence = ["reference list is empty", *hints]
        return out

    analysed = [analyse_reference(paper, r) for r in work.references]
    matched = [a for a in analysed if a.about_paper]
    if not matched:
        out.evidence = [
            f"none of the {len(work.references)} deposited references matches the "
            "tracked paper (reference list may be incomplete)",
            *hints,
        ]
        return out

    pre = [e for a in matched for e in a.preprint]
    vor_pub = [e for a in matched for e in a.vor_publisher]
    vor_cr = [e for a in matched for e in a.vor_crossref]
    out.matched_reference = matched[0].ref.unstructured or matched[0].ref.text()
    out.evidence += [f"matched reference via {', '.join(matched[0].identity)}"]

    if pre and vor_pub:
        out.target_version = TargetVersion.BOTH
        out.signals = ["preprint_explicit", "vor_explicit"]
        out.evidence += pre + vor_pub
    elif pre:
        out.target_version = TargetVersion.PREPRINT
        out.signals = ["preprint_explicit"]
        out.evidence += pre
        if vor_cr:
            out.signals.append("vor_crossref_matched")
            out.evidence += [
                *vor_cr,
                "the final DOI was attached by Crossref's reference matcher, not by the "
                "publisher; the reference itself cites the preprint",
            ]
        if paper.journal_doi and not any(paper.journal_doi in e for e in vor_cr):
            out.evidence.append("reference does not contain the final DOI")
    elif vor_pub or vor_cr:
        out.target_version = TargetVersion.VERSION_OF_RECORD
        out.signals = ["vor_explicit"] if vor_pub else ["vor_crossref_matched"]
        out.evidence += vor_pub + vor_cr
    else:
        ref = matched[0].ref
        ref_year = int(ref.year) if ref.year and ref.year[:4].isdigit() else None
        py = arxiv_year(paper.arxiv_id)
        if (
            ref_year
            and py
            and ref_year == py
            and (paper.year is None or ref_year < paper.year)
            and not ref.journal_title
            and not ref.volume
        ):
            out.target_version = TargetVersion.PREPRINT
            out.signals = ["preprint_year_only"]
            out.evidence += [
                f"reference year {ref_year} matches the preprint year and no "
                "journal metadata is present (weak signal)"
            ]
        else:
            out.evidence += ["matched reference carries no version-specific metadata"]
    out.evidence += hints
    return out


def apply_manual_target(auto: TargetClassification, manual: dict) -> TargetClassification:
    """Override the cited version with a user-confirmed one, keeping the automatic evidence."""
    note = f" ({manual['note']})" if manual.get("note") else ""
    return TargetClassification(
        target_version=TargetVersion(manual["target"]),
        evidence=[
            f"confirmed manually by the user: reference cites {manual['target']}{note}",
            *[f"automatic: {e}" for e in auto.evidence],
        ],
        matched_reference=auto.matched_reference,
        signals=["manual_confirmed"],
    )


# --------------------------------------------------------------------------- Scopus reference links


@dataclass
class ScopusRefLink:
    status: LinkageStatus
    evidence: list[str]
    reference: ReferenceEntry | None = None
    linked_eid: str | None = None  # record Scopus attached the reference to


_REF_SUFFIX_RE = re.compile(
    r"[.,;:\s]*(?:arxiv(?:\s+preprint)?(?:\s*arxiv:\S+)?|preprint)\s*$", re.IGNORECASE
)


def _ref_title_candidates(ref: object) -> list[str]:
    """Scopus puts the cited title in ``title`` or, for unresolved references, in
    ``sourcetitle`` (often with an "arXiv preprint" suffix)."""
    out = []
    for value in (getattr(ref, "title", None), getattr(ref, "sourcetitle", None)):
        if value:
            out.append(_REF_SUFFIX_RE.sub("", value).strip())
    return [v for v in out if v]


def match_scopus_reference(paper: TrackedPaper, refs: list) -> object | None:
    """The entry of a Scopus reference list that cites ``paper`` (any version)."""
    dois = {d for d in (paper.journal_doi, paper.arxiv_doi) if d}
    first = author_surname(paper.authors[0]) if paper.authors else ""
    best, best_score = None, 0.0
    for ref in refs:
        if ref.doi and ref.doi in dois:
            return ref
        if not paper.title:
            continue
        score = 0.0
        for cand in _ref_title_candidates(ref):
            s = title_similarity(paper.title, cand)
            if len(normalize_title(cand)) >= 20:
                # The preprint title is often a prefix of the published title (or vice versa).
                s = max(s, title_in_text(cand, paper.title), title_in_text(paper.title, cand))
            score = max(score, s)
        surnames = {author_surname(a) for a in ref.authors}
        if first and ref.authors and first not in surnames:
            score -= 10
        if score > best_score:
            best, best_score = ref, score
    return best if best_score >= REFERENCE_TITLE_THRESHOLD else None


def scopus_reference_link(
    paper: TrackedPaper, refs: list | None, vor_eid: str | None
) -> ScopusRefLink | None:
    """Per-article linkage from the citing record's Scopus reference list."""
    if refs is None:
        return None
    ref = match_scopus_reference(paper, refs)
    if ref is None:
        return ScopusRefLink(
            LinkageStatus.UNVERIFIED,
            [f"no entry matching the paper in the Scopus reference list ({len(refs)} entries)"],
        )
    entry = ReferenceEntry(
        doi=ref.doi,
        article_title=ref.title,
        journal_title=ref.sourcetitle,
        year=str(ref.year) if ref.year else None,
        author=", ".join(ref.authors[:3]) or None,
        source="scopus",
        matched=True,
        unstructured=f"[Scopus reference #{ref.position}] "
        + " ".join(x for x in (ref.title, ref.sourcetitle) if x),
    )
    shown = ref.title or ref.sourcetitle or "untitled"
    source = ref.sourcetitle if ref.title else None
    where = f"Scopus reference #{ref.position} ('{shown}'" + (f", {source})" if source else ")")
    if not vor_eid:
        return ScopusRefLink(
            LinkageStatus.UNVERIFIED,
            [f"{where} found, but the Version of Record EID is unknown"],
            entry,
            ref.eid,
        )
    if ref.eid == vor_eid:
        return ScopusRefLink(
            LinkageStatus.LINKED_TO_VOR,
            [f"{where} is linked to the Version of Record {vor_eid}"],
            entry,
            ref.eid,
        )
    target = f"record {ref.eid} ({ref.ref_type})" if ref.eid else "no Scopus record"
    return ScopusRefLink(
        LinkageStatus.NOT_LINKED,
        [f"{where} is linked to {target}, not to the Version of Record {vor_eid}"],
        entry,
        ref.eid,
    )


# --------------------------------------------------------------------------- Scopus count check


@dataclass
class CountAnalysis:
    """Compare Scopus' cited-by count of the Version of Record with the citing articles
    confirmed in Scopus.

    Pigeonhole argument: if ``confirmed`` Scopus-indexed articles cite the work but Scopus
    counts only ``citedby`` citations for the Version of Record, at least
    ``confirmed - citedby`` of them are not linked to it (whatever else Scopus counts).
    """

    citedby: int
    confirmed: int
    explicit_vor: int  # confirmed citers whose reference carries the final DOI explicitly
    deficit: int
    all_others_unlinked: bool
    summary: str


def analyse_counts(citedby: int | None, confirmed: int, explicit_vor: int) -> CountAnalysis | None:
    if citedby is None or confirmed == 0:
        return None
    deficit = max(0, confirmed - citedby)
    # If every Scopus citation can be accounted for by explicit final-DOI references,
    # none of the remaining confirmed citers can be linked.
    all_others = deficit > 0 and citedby <= explicit_vor
    if deficit:
        summary = (
            f"Scopus counts {citedby} citation(s) for the Version of Record, but {confirmed} "
            f"Scopus-indexed article(s) cite this work: at least {deficit} citation(s) are "
            "not linked to the Version of Record"
        )
    else:
        summary = (
            f"Scopus counts {citedby} citation(s) for the Version of Record and {confirmed} "
            "Scopus-indexed citing article(s) were found: the count alone does not show "
            "missing links"
        )
    return CountAnalysis(citedby, confirmed, explicit_vor, deficit, all_others, summary)


# --------------------------------------------------------------------------- actions & confidence


def decide_action(
    ptype: PublicationType,
    target: TargetVersion,
    src: ScopusSourceStatus,
    art: ScopusArticleStatus,
    linkage: LinkageStatus,
    vor_explicit: bool = True,
) -> tuple[Action, str]:
    """Map statuses to a recommended action.

    ``vor_explicit`` is False when the Version of Record was only inferred (e.g. the DOI was
    attached by Crossref's matcher); such citations are never auto-marked ``OK`` without a
    Scopus linkage check.
    """
    explicit_vor = target == TargetVersion.VERSION_OF_RECORD and vor_explicit
    if ptype == PublicationType.PREPRINT:
        return Action.PREPRINT_ONLY, "citing work is only available as a preprint"
    if art == ScopusArticleStatus.SCOPUS_CONFIRMED:
        if linkage == LinkageStatus.NOT_LINKED:
            return (
                Action.LIKELY_MISSING_LINK,
                "indexed in Scopus but absent from the Version of Record's Scopus citers",
            )
        if linkage == LinkageStatus.LINKED_TO_VOR:
            return Action.OK, "Scopus already counts this citation for the Version of Record"
        if explicit_vor:
            return Action.OK, "indexed in Scopus and cites the final DOI explicitly"
        return Action.CHECK_SCOPUS, "indexed in Scopus; linkage to the final version unverified"
    if art == ScopusArticleStatus.SCOPUS_NOT_FOUND:
        if src in (*COVERED, ScopusSourceStatus.SERIES_PREVIOUSLY_INDEXED):
            return (
                Action.CHECK_SCOPUS,
                "source is Scopus-covered but the article was not found (may be pending)",
            )
        return Action.NON_SCOPUS, "article not found in Scopus and source not covered"
    if src in NOT_COVERED:
        return Action.NON_SCOPUS, "source not covered by Scopus for this year"
    if src in COVERED:
        if explicit_vor:
            return Action.OK, "Scopus-covered source; reference cites the final DOI explicitly"
        return Action.CHECK_SCOPUS, "Scopus-covered source; verify linkage in Scopus"
    if src == ScopusSourceStatus.SERIES_PREVIOUSLY_INDEXED:
        return (
            Action.CHECK_SCOPUS,
            "earlier editions of this conference are Scopus-indexed; verify this edition",
        )
    if ptype == PublicationType.THESIS:
        return Action.NON_SCOPUS, "theses/dissertations are generally not indexed by Scopus"
    return Action.UNKNOWN, "Scopus coverage could not be determined"


def score_confidence(
    *,
    work: WorkRecord,
    discovered_via: list[str],
    identity_methods: list[str],
    warnings: list[str],
    ambiguous: bool,
    target: TargetClassification,
    src: SourceMatch,
    art: ScopusArticleCheck,
    linkage: LinkageStatus,
    action: Action,
    linkage_deduced: bool = False,
) -> tuple[float, list[Evidence]]:
    ev: list[Evidence] = [Evidence(signal="baseline", weight=0.30, detail="deterministic prior")]

    def add(signal: str, weight: float, detail: str = "") -> None:
        ev.append(Evidence(signal=signal, weight=weight, detail=detail))

    if work.ids.doi and "crossref" in work.sources:
        add("doi_exact", 0.15, f"DOI {work.ids.doi} resolved in Crossref")
    elif work.ids.doi:
        add("doi_present", 0.08, f"DOI {work.ids.doi} (not confirmed in Crossref)")
    independent = set(discovered_via)
    if len(independent) >= 2:
        add("multi_source", 0.10, f"found in {', '.join(sorted(independent))}")
    if identity_methods and all(m.startswith(("fuzzy", "title")) for m in identity_methods):
        add("fuzzy_title_only", -0.15, "records merged by fuzzy title only")
    elif any(m.startswith(("fuzzy", "title")) for m in identity_methods):
        add("fuzzy_title_merge", -0.05, "some provider records merged by title similarity")
    if any("year mismatch" in w for w in warnings):
        add("year_mismatch", -0.10, "; ".join(w for w in warnings if "year" in w))
    if any("conflicting" in w for w in warnings):
        add("conflicting_ids", -0.20, "; ".join(w for w in warnings if "conflict" in w))
    if ambiguous:
        add("ambiguous_duplicate", -0.10, "possible duplicate needs manual review")

    tv = target.target_version
    if "preprint_explicit" in target.signals:
        add("reference_cites_preprint", 0.15, "bibliography entry explicitly cites the preprint")
    if "vor_explicit" in target.signals:
        add("reference_cites_final_doi", 0.15, "bibliography entry carries the final DOI")
    if "manual_confirmed" in target.signals:
        add("manual_confirmation", 0.15, "cited version confirmed manually by the user")
    if "preprint_year_only" in target.signals:
        add("weak_preprint_signal", 0.03, "only the reference year suggests the preprint")
    if tv == TargetVersion.UNKNOWN:
        add("target_unknown", -0.10, "no bibliography data to tell which version is cited")

    if src.matched_on and src.matched_on.startswith(("issn", "isbn")):
        add("source_id_exact", 0.10, f"Scopus Source List matched on {src.matched_on}")
    elif src.matched_on in ("title", "conference_title"):
        add("source_title_match", 0.03, "Scopus Source List matched on normalised title only")
    if src.status == ScopusSourceStatus.SERIES_PREVIOUSLY_INDEXED:
        add("series_heuristic", -0.05, "coverage inferred from earlier conference editions")
    if src.status == ScopusSourceStatus.NO_SOURCE_ID:
        add("ambiguous_source", -0.10, "venue could not be matched to the Scopus Source List")
    if src.status == ScopusSourceStatus.LIST_NOT_LOADED:
        add("source_list_missing", -0.10, "Scopus Source List not loaded")

    if art.status == ScopusArticleStatus.SCOPUS_CONFIRMED:
        add("scopus_api_confirmed", 0.20, art.detail)
    elif art.status == ScopusArticleStatus.SCOPUS_NOT_FOUND:
        add("scopus_api_not_found", 0.05, art.detail)
    if linkage_deduced:
        add(
            "linkage_deduced_from_count",
            0.05,
            "missing link deduced from the Scopus cited-by count (not a per-article check)",
        )
    elif linkage in (LinkageStatus.LINKED_TO_VOR, LinkageStatus.NOT_LINKED):
        add("linkage_verified", 0.10, f"Scopus cited-by comparison: {linkage.value}")
    if action == Action.CHECK_SCOPUS and art.status != ScopusArticleStatus.SCOPUS_CONFIRMED:
        add("no_article_level_check", -0.05, "article-level Scopus indexing not verified")

    score = sum(e.weight for e in ev)
    return round(min(1.0, max(0.0, score)), 2), ev


def reconcile(
    *,
    work: WorkRecord,
    discovered_via: list[str],
    target: TargetClassification,
    src: SourceMatch,
    art: ScopusArticleCheck,
    linkage: LinkageStatus,
    identity_methods: list[str],
    warnings: list[str],
    review_reasons: list[str],
    linkage_evidence: list[str] | None = None,
) -> ReconciliationResult:
    action, why = decide_action(
        work.publication_type,
        target.target_version,
        src.status,
        art.status,
        linkage,
        vor_explicit=bool({"vor_explicit", "manual_confirmed"} & set(target.signals)),
    )
    confidence, ev = score_confidence(
        work=work,
        discovered_via=discovered_via,
        identity_methods=identity_methods,
        warnings=warnings,
        ambiguous=bool(review_reasons),
        target=target,
        src=src,
        art=art,
        linkage=linkage,
        action=action,
        linkage_deduced=linkage == LinkageStatus.NOT_LINKED
        and any("accounted for" in e for e in linkage_evidence or []),
    )
    reasons = list(review_reasons)
    if action == Action.UNKNOWN:
        reasons.append("coverage could not be determined")
    if identity_methods and all(m.startswith(("fuzzy", "title")) for m in identity_methods):
        reasons.append("identity based on fuzzy title match only")
    reasons += [w for w in warnings if "conflict" in w or "year mismatch" in w]
    discovery = ", ".join(discovered_via) or "unknown"
    return ReconciliationResult(
        discovery_status=discovery,
        publication_status=work.publication_type,
        scopus_source_status=src.status,
        scopus_article_status=art.status,
        target_version=target.target_version,
        linkage_status=linkage,
        action=action,
        confidence=confidence,
        confidence_evidence=ev,
        target_evidence=target.evidence,
        source_evidence=[*src.evidence, *(linkage_evidence or []), f"action: {why}"],
        scopus_detail=art.detail,
        needs_review=bool(reasons),
        review_reasons=reasons,
    )

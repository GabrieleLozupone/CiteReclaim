"""Identifier normalisation, fuzzy matching and entity resolution."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .models import PublicationType, WorkRecord

# --------------------------------------------------------------------------- thresholds

TITLE_ONLY_THRESHOLD = 96.0  # fuzzy title match accepted on its own
TITLE_WITH_CORROBORATION_THRESHOLD = 90.0  # accepted only if year AND author agree
AMBIGUOUS_FLOOR = 85.0  # between floor and acceptance → flag for manual review

ARXIV_DOI_PREFIX = "10.48550/arxiv."

# DOI prefixes of well-known preprint servers.
PREPRINT_DOI_PREFIXES = (
    "10.48550/",  # arXiv (DataCite)
    "10.1101/",  # bioRxiv / medRxiv
    "10.2139/ssrn",  # SSRN
    "10.21203/rs.",  # Research Square
    "10.20944/preprints",  # Preprints.org
    "10.31219/osf.io",  # OSF Preprints
    "10.31234/osf.io",  # PsyArXiv
    "10.36227/techrxiv",  # TechRxiv
    "10.22541/au.",  # Authorea
    "10.26434/chemrxiv",  # ChemRxiv
    "10.5281/zenodo",  # Zenodo deposits (often preprints / code)
)

# --------------------------------------------------------------------------- DOI

_DOI_PREFIX_RE = re.compile(r"^(?:https?://)?(?:dx\.)?(?:doi\.org/|doi:\s*|doi\s+)", re.IGNORECASE)
_DOI_IN_TEXT_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>{}]+)", re.IGNORECASE)


def normalize_doi(value: str | None) -> str | None:
    """Return a canonical lowercase DOI or ``None`` if ``value`` is not a DOI."""
    if not value:
        return None
    doi = value.strip()
    # Repeatedly strip resolver prefixes ("https://doi.org/doi:10...").
    while True:
        stripped = _DOI_PREFIX_RE.sub("", doi, count=1).strip()
        if stripped == doi:
            break
        doi = stripped
    doi = doi.strip().rstrip(".,;:)]}>'\"").strip()
    doi = re.sub(r"\s+", "", doi).lower()
    if not re.match(r"^10\.\d{4,9}/\S+$", doi):
        return None
    return doi


def find_dois(text: str | None) -> list[str]:
    if not text:
        return []
    out = []
    for m in _DOI_IN_TEXT_RE.finditer(text):
        doi = normalize_doi(m.group(1))
        if doi and doi not in out:
            out.append(doi)
    return out


def is_preprint_doi(doi: str | None) -> bool:
    doi = normalize_doi(doi)
    return bool(doi) and doi.startswith(PREPRINT_DOI_PREFIXES)


# --------------------------------------------------------------------------- arXiv

_ARXIV_NEW_RE = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?(?![\d])")
_ARXIV_OLD_RE = re.compile(r"\b([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?\b", re.IGNORECASE)
_ARXIV_CONTEXT_RE = re.compile(
    r"(?:arxiv(?:\.org)?(?:/abs|/pdf)?[:/\s]*|10\.48550/arxiv\.)"
    r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})",
    re.IGNORECASE,
)


def normalize_arxiv_id(value: str | None) -> str | None:
    """Normalise ``arXiv:2504.08635v2`` / URLs / DOIs to ``2504.08635``."""
    if not value:
        return None
    v = value.strip()
    v = re.sub(r"^(?:https?://)?(?:www\.)?(?:export\.)?arxiv\.org/(?:abs|pdf)/", "", v, flags=re.I)
    v = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", v, flags=re.I)
    v = re.sub(r"^10\.48550/arxiv\.", "", v, flags=re.I)
    v = re.sub(r"^arxiv:\s*", "", v, flags=re.I)
    v = re.sub(r"\.pdf$", "", v, flags=re.I)
    v = re.sub(r"v\d+$", "", v.strip())
    if re.fullmatch(r"\d{4}\.\d{4,5}", v):
        return v
    if re.fullmatch(r"[a-z\-]+(?:\.[a-z]{2})?/\d{7}", v, flags=re.I):
        return v.lower()
    return None


def arxiv_doi(arxiv_id: str) -> str:
    return f"{ARXIV_DOI_PREFIX}{arxiv_id.lower()}"


def find_arxiv_ids(text: str | None, *, require_context: bool = True) -> list[str]:
    """Extract arXiv identifiers from free text.

    With ``require_context`` (default) the id must be introduced by ``arXiv``,
    an arxiv.org URL or the arXiv DOI prefix, which avoids mistaking page ranges
    such as ``1234.5678`` for arXiv ids.
    """
    if not text:
        return []
    found: list[str] = []
    pattern = _ARXIV_CONTEXT_RE if require_context else _ARXIV_NEW_RE
    for m in pattern.finditer(text):
        aid = normalize_arxiv_id(m.group(1))
        if aid and aid not in found:
            found.append(aid)
    return found


# --------------------------------------------------------------------------- ISSN / ISBN


def normalize_issn(value: str | None) -> str | None:
    """Return ``NNNN-NNNX`` or ``None``. Accepts ``12345678``, ``1234-567x`` etc."""
    if value is None:
        return None
    v = re.sub(r"[^0-9Xx]", "", str(value)).upper()
    if not v:
        return None
    if len(v) < 8 and v.isdigit():
        v = v.zfill(8)  # spreadsheets frequently drop leading zeros
    if len(v) != 8 or not re.fullmatch(r"\d{7}[\dX]", v):
        return None
    return f"{v[:4]}-{v[4:]}"


def normalize_isbn(value: str | int | None) -> str | None:
    if value is None:
        return None
    v = re.sub(r"[^0-9Xx]", "", str(value)).upper()
    if len(v) == 13 or len(v) == 10:
        return v
    return None


# --------------------------------------------------------------------------- titles / authors

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def normalize_title(value: str | None) -> str:
    """Unicode-normalise, strip markup and punctuation, lowercase, collapse spaces."""
    if not value:
        return ""
    v = _TAG_RE.sub(" ", value)
    v = unicodedata.normalize("NFKD", v)
    v = "".join(ch for ch in v if not unicodedata.combining(ch))
    v = v.casefold().replace("_", " ")
    v = _PUNCT_RE.sub(" ", v)
    return _WS_RE.sub(" ", v).strip()


def title_similarity(a: str | None, b: str | None) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    return float(fuzz.ratio(na, nb))


def title_in_text(title: str | None, text: str | None) -> float:
    """How well ``title`` appears inside a longer reference string (0–100)."""
    nt, nx = normalize_title(title), normalize_title(text)
    if not nt or not nx:
        return 0.0
    if nt in nx:
        return 100.0
    if len(nt) < 20:  # short titles are too ambiguous for partial matching
        return 0.0
    return float(fuzz.partial_ratio(nt, nx))


def author_surname(name: str | None) -> str:
    """Best-effort surname extraction for 'Lozupone, G.', 'G. Lozupone', 'Gabriele Lozupone'."""
    if not name:
        return ""
    n = name.strip()
    if "," in n:
        n = n.split(",", 1)[0]
    else:
        parts = [p for p in re.split(r"\s+", n) if p]
        # Drop initials ("G.", "G") and pick the last remaining token.
        parts = [p for p in parts if len(p.strip(".")) > 1] or parts
        n = parts[-1] if parts else ""
    return normalize_title(n).replace(" ", "")


def author_overlap(a: list[str], b: list[str]) -> float:
    """Fraction of surnames in the shorter list present in the other (0–1)."""
    sa = {author_surname(x) for x in a if author_surname(x)}
    sb = {author_surname(x) for x in b if author_surname(x)}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def first_author_matches(a: list[str], b: list[str]) -> bool:
    return bool(a and b and author_surname(a[0]) and author_surname(a[0]) == author_surname(b[0]))


# --------------------------------------------------------------------------- record matching


@dataclass
class MatchDecision:
    matched: bool
    ambiguous: bool = False
    method: str = ""
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _years_agree(a: int | None, b: int | None, tolerance: int = 1) -> bool | None:
    if a is None or b is None:
        return None
    return abs(a - b) <= tolerance


def compare_records(a: WorkRecord, b: WorkRecord) -> MatchDecision:
    """Decide whether two records describe the same work.

    Priority: DOI > PMID > arXiv > S2/OpenAlex id > fuzzy title with corroboration.
    Conflicting non-preprint DOIs never merge.
    """
    ia, ib = a.ids, b.ids
    if ia.doi and ib.doi and ia.doi == ib.doi:
        return MatchDecision(True, method="doi", score=100, reasons=[f"same DOI {ia.doi}"])
    for name in ("pmid", "arxiv", "s2", "openalex", "scopus_eid"):
        va, vb = getattr(ia, name), getattr(ib, name)
        if va and vb and va == vb:
            return MatchDecision(True, method=name, score=100, reasons=[f"same {name} {va}"])

    doi_conflict = bool(
        ia.doi
        and ib.doi
        and ia.doi != ib.doi
        and not (is_preprint_doi(ia.doi) or is_preprint_doi(ib.doi))
    )
    score = title_similarity(a.title, b.title)
    if score < AMBIGUOUS_FLOOR:
        return MatchDecision(False, score=score)

    years = _years_agree(a.year, b.year)
    overlap = author_overlap(a.authors, b.authors)
    first = first_author_matches(a.authors, b.authors)
    reasons = [f"title similarity {score:.1f}"]
    if years is not None:
        reasons.append(f"years {a.year}/{b.year} {'agree' if years else 'disagree'}")
    if a.authors and b.authors:
        reasons.append(f"author overlap {overlap:.2f}")

    if doi_conflict:
        return MatchDecision(
            False,
            ambiguous=score >= TITLE_ONLY_THRESHOLD,
            score=score,
            reasons=[*reasons, f"conflicting DOIs {ia.doi} vs {ib.doi}"],
        )
    if years is False and score < 100:
        return MatchDecision(False, ambiguous=True, score=score, reasons=reasons)
    # Preprint/journal pairs of one work are merged (a published citing work
    # that also has an arXiv version) only with author corroboration.
    preprint_pair = bool(ia.doi and ib.doi and ia.doi != ib.doi)
    if preprint_pair:
        if score >= TITLE_ONLY_THRESHOLD and (overlap >= 0.5 or first):
            return MatchDecision(
                True, method="title+authors (preprint/journal pair)", score=score, reasons=reasons
            )
        return MatchDecision(False, ambiguous=True, score=score, reasons=reasons)
    if score >= TITLE_ONLY_THRESHOLD and years is not False:
        if a.authors and b.authors and overlap == 0:
            return MatchDecision(
                False, ambiguous=True, score=score, reasons=[*reasons, "no author overlap"]
            )
        return MatchDecision(True, method="fuzzy_title", score=score, reasons=reasons)
    if score >= TITLE_WITH_CORROBORATION_THRESHOLD and years and (first or overlap >= 0.5):
        return MatchDecision(True, method="fuzzy_title+year+author", score=score, reasons=reasons)
    return MatchDecision(False, ambiguous=True, score=score, reasons=reasons)


# --------------------------------------------------------------------------- merging

_TYPE_RANK = {
    PublicationType.UNKNOWN: 0,
    PublicationType.PREPRINT: 1,
    PublicationType.THESIS: 2,
    PublicationType.BOOK: 2,
    PublicationType.BOOK_CHAPTER: 3,
    PublicationType.CONFERENCE_PAPER: 4,
    PublicationType.JOURNAL_ARTICLE: 5,
}


def merge_records(base: WorkRecord, other: WorkRecord) -> list[str]:
    """Merge ``other`` into ``base`` in place. Returns warnings (e.g. conflicts)."""
    warnings: list[str] = []
    # Keep a non-preprint DOI as the canonical DOI; demote arXiv DOI to arxiv id.
    if base.ids.doi and other.ids.doi and base.ids.doi != other.ids.doi:
        if is_preprint_doi(base.ids.doi) and not is_preprint_doi(other.ids.doi):
            base.extra.setdefault("alt_dois", []).append(base.ids.doi)
            base.ids.doi = other.ids.doi
        else:
            base.extra.setdefault("alt_dois", []).append(other.ids.doi)
    conflicts = base.ids.merge(other.ids)
    conflicts = [c for c in conflicts if c != "doi"]
    if conflicts:
        warnings.append(f"conflicting identifiers: {', '.join(conflicts)}")
    if not base.title or (
        other.title and len(other.title) > len(base.title) and "crossref" in other.sources
    ):
        base.title = other.title or base.title
    if not base.authors and other.authors:
        base.authors = other.authors
    if other.year and base.year and abs(other.year - base.year) > 1:
        warnings.append(f"year mismatch {base.year} vs {other.year}")
    if other.year and (not base.year or "crossref" in other.sources):
        base.year = other.year
    if other.venue and (not base.venue or "crossref" in other.sources):
        base.venue = other.venue
    if other.publisher and not base.publisher:
        base.publisher = other.publisher
    for issn in other.issns:
        if issn not in base.issns:
            base.issns.append(issn)
    for isbn in other.isbns:
        if isbn not in base.isbns:
            base.isbns.append(isbn)
    base.raw_types.update(other.raw_types)
    if _TYPE_RANK[other.publication_type] > _TYPE_RANK[base.publication_type]:
        base.publication_type = other.publication_type
    for s in other.sources:
        if s not in base.sources:
            base.sources.append(s)
    for d in other.discovered_against:
        if d not in base.discovered_against:
            base.discovered_against.append(d)
    if other.references is not None and (
        base.references is None or len(other.references) > len(base.references)
    ):
        base.references = other.references
    if other.referenced_openalex_ids is not None:
        base.referenced_openalex_ids = sorted(
            set(base.referenced_openalex_ids or []) | set(other.referenced_openalex_ids)
        )
    base.s2_contexts.extend(c for c in other.s2_contexts if c not in base.s2_contexts)
    for k, v in other.extra.items():
        base.extra.setdefault(k, v)
    return warnings


@dataclass
class Cluster:
    record: WorkRecord
    members: list[WorkRecord] = field(default_factory=list)
    merge_methods: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)

    @property
    def fuzzy_only(self) -> bool:
        return bool(self.merge_methods) and all(
            m.startswith("fuzzy") or m.startswith("title") for m in self.merge_methods
        )


class EntityResolver:
    """Incrementally cluster records from several providers into canonical works."""

    def __init__(self) -> None:
        self.clusters: list[Cluster] = []

    def add(self, record: WorkRecord) -> Cluster:
        exact: list[Cluster] = []
        fuzzy: list[tuple[Cluster, MatchDecision]] = []
        ambiguous: list[tuple[Cluster, MatchDecision]] = []
        for cl in self.clusters:
            d = compare_records(cl.record, record)
            if d.matched and d.method in ("doi", "pmid", "arxiv", "s2", "openalex", "scopus_eid"):
                exact.append(cl)
            elif d.matched:
                fuzzy.append((cl, d))
            elif d.ambiguous:
                ambiguous.append((cl, d))

        if exact:
            target = exact[0]
            target.warnings += merge_records(target.record, record)
            target.members.append(record)
            target.merge_methods.append("exact_id")
            # An identifier may bridge two clusters built from different ids.
            for extra in exact[1:]:
                target.warnings += merge_records(target.record, extra.record)
                target.members += extra.members
                target.merge_methods += extra.merge_methods
                self.clusters.remove(extra)
            return target
        if len(fuzzy) == 1 and not ambiguous:
            cl, d = fuzzy[0]
            cl.warnings += merge_records(cl.record, record)
            cl.members.append(record)
            cl.merge_methods.append(d.method)
            return cl
        new = Cluster(record=record.model_copy(deep=True), members=[record])
        candidates = [c for c, _ in fuzzy] + [c for c, _ in ambiguous]
        if candidates:
            reasons = "; ".join(
                f"'{c.record.label()[:60]}' ({', '.join(d.reasons)})" for c, d in fuzzy + ambiguous
            )
            msg = f"possible duplicate of: {reasons}"
            new.review_reasons.append(msg)
            for c in candidates:
                c.review_reasons.append(f"possible duplicate of '{record.label()[:60]}'")
        self.clusters.append(new)
        return new

    def merge_by_doi(self) -> int:
        """Merge clusters that share a DOI (e.g. after DOI resolution). Returns merges."""
        by_doi: dict[str, Cluster] = {}
        merged = 0
        for cl in list(self.clusters):
            doi = cl.record.ids.doi
            if not doi:
                continue
            if doi in by_doi:
                keep = by_doi[doi]
                keep.warnings += merge_records(keep.record, cl.record)
                keep.members += cl.members
                keep.merge_methods += [*cl.merge_methods, "exact_id"]
                keep.review_reasons += cl.review_reasons
                self.clusters.remove(cl)
                merged += 1
            else:
                by_doi[doi] = cl
        return merged


def confident_title_match(
    rec: WorkRecord, cand: WorkRecord | None, score: float | None = None
) -> bool:
    """Conservative acceptance of a title-search candidate as the same work."""
    if not cand or not cand.title or not rec.title:
        return False
    s = score if score is not None else title_similarity(rec.title, cand.title)
    years_ok = not rec.year or not cand.year or abs(rec.year - cand.year) <= 1
    if rec.authors and cand.authors and author_overlap(rec.authors, cand.authors) == 0:
        return False
    if s >= TITLE_ONLY_THRESHOLD and years_ok:
        return True
    return (
        s >= TITLE_WITH_CORROBORATION_THRESHOLD
        and bool(rec.year and cand.year and years_ok)
        and (not rec.authors or not cand.authors or first_author_matches(rec.authors, cand.authors))
    )

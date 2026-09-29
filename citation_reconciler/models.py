"""Domain models shared across providers, matching and reconciliation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class PublicationType(StrEnum):
    JOURNAL_ARTICLE = "journal_article"
    CONFERENCE_PAPER = "conference_paper"
    PREPRINT = "preprint"
    THESIS = "thesis"
    BOOK = "book"
    BOOK_CHAPTER = "book_chapter"
    UNKNOWN = "unknown"


class TargetVersion(StrEnum):
    PREPRINT = "PREPRINT"
    VERSION_OF_RECORD = "VERSION_OF_RECORD"
    BOTH = "BOTH"
    UNKNOWN = "UNKNOWN"


class ScopusSourceStatus(StrEnum):
    COVERED = "COVERED"  # source in list and the citing year is inside coverage
    COVERED_TITLE_MATCH = "COVERED_TITLE_MATCH"  # matched by normalised title only
    SERIES_PREVIOUSLY_INDEXED = "SERIES_PREVIOUSLY_INDEXED"  # earlier conference editions indexed
    OUTSIDE_COVERAGE = "OUTSIDE_COVERAGE"  # source in list, year outside coverage
    NOT_IN_LIST = "NOT_IN_LIST"  # ISSN/ISBN known and absent from the list
    NO_SOURCE_ID = "NO_SOURCE_ID"  # no ISSN/ISBN/title to match
    LIST_NOT_LOADED = "LIST_NOT_LOADED"


class ScopusArticleStatus(StrEnum):
    SCOPUS_CONFIRMED = "SCOPUS_CONFIRMED"
    SCOPUS_SOURCE_ONLY = "SCOPUS_SOURCE_ONLY"
    SCOPUS_NOT_FOUND = "SCOPUS_NOT_FOUND"
    SCOPUS_API_UNAVAILABLE = "SCOPUS_API_UNAVAILABLE"
    SCOPUS_PERMISSION_DENIED = "SCOPUS_PERMISSION_DENIED"
    UNKNOWN = "UNKNOWN"


class LinkageStatus(StrEnum):
    LINKED_TO_VOR = "LINKED_TO_VOR"  # Scopus lists the citing doc among VoR citers
    NOT_LINKED = "NOT_LINKED"  # Scopus-confirmed citing doc absent from VoR citers
    UNVERIFIED = "UNVERIFIED"  # comparison impossible with available data
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Action(StrEnum):
    OK = "OK"
    CHECK_SCOPUS = "CHECK_SCOPUS"
    LIKELY_MISSING_LINK = "LIKELY_MISSING_LINK"
    NON_SCOPUS = "NON_SCOPUS"
    PREPRINT_ONLY = "PREPRINT_ONLY"
    UNKNOWN = "UNKNOWN"


class Evidence(BaseModel):
    """One contributing signal of a confidence score."""

    signal: str
    weight: float
    detail: str = ""


class ExternalIds(BaseModel):
    doi: str | None = None
    arxiv: str | None = None
    pmid: str | None = None
    openalex: str | None = None
    s2: str | None = None
    scopus_eid: str | None = None
    dblp: str | None = None

    def merge(self, other: ExternalIds) -> list[str]:
        """Fill missing ids from ``other``; return names of conflicting fields."""
        conflicts = []
        for name in type(self).model_fields:
            mine, theirs = getattr(self, name), getattr(other, name)
            if theirs and not mine:
                setattr(self, name, theirs)
            elif theirs and mine and mine != theirs:
                conflicts.append(name)
        return conflicts


class ReferenceEntry(BaseModel):
    """A bibliography entry of a citing work (usually from Crossref)."""

    doi: str | None = None
    doi_asserted_by: str | None = None
    unstructured: str | None = None
    article_title: str | None = None
    journal_title: str | None = None
    volume: str | None = None
    first_page: str | None = None
    year: str | None = None
    author: str | None = None
    source: str = "crossref"
    matched: bool = False  # already identified as citing the tracked paper (e.g. by Scopus)

    def text(self) -> str:
        parts = [
            self.author,
            self.article_title,
            self.journal_title,
            self.volume,
            self.first_page,
            self.year,
            self.unstructured,
        ]
        return " ".join(p for p in parts if p)


class WorkRecord(BaseModel):
    """A bibliographic record as returned by one provider (or merged)."""

    ids: ExternalIds = Field(default_factory=ExternalIds)
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    issns: list[str] = Field(default_factory=list)
    isbns: list[str] = Field(default_factory=list)
    publisher: str | None = None
    publication_type: PublicationType = PublicationType.UNKNOWN
    raw_types: dict[str, str] = Field(default_factory=dict)  # provider -> native type
    sources: list[str] = Field(default_factory=list)  # providers / discovery channels
    references: list[ReferenceEntry] | None = None  # None = not available
    referenced_openalex_ids: list[str] | None = None
    # Which tracked-paper version record the provider reported the citation against.
    discovered_against: list[str] = Field(default_factory=list)
    s2_contexts: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    def label(self) -> str:
        return self.title or self.ids.doi or self.ids.arxiv or "<untitled>"


class TrackedPaper(BaseModel):
    """A paper whose citations the user wants to reconcile."""

    id: int | None = None
    name: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    arxiv_id: str | None = None
    arxiv_doi: str | None = None
    journal_doi: str | None = None
    journal_title: str | None = None
    journal_issns: list[str] = Field(default_factory=list)
    volume: str | None = None
    pages: str | None = None
    year: int | None = None
    # Provider-specific ids for the two versions (filled during sync).
    version_ids: dict[str, dict[str, str]] = Field(default_factory=dict)


class TargetClassification(BaseModel):
    target_version: TargetVersion = TargetVersion.UNKNOWN
    evidence: list[str] = Field(default_factory=list)
    matched_reference: str | None = None
    signals: list[str] = Field(default_factory=list)  # machine-readable signal names


class SourceMatch(BaseModel):
    status: ScopusSourceStatus
    source_title: str | None = None
    sourcerecord_id: str | None = None
    matched_on: str | None = None  # "issn:xxxx" / "isbn:..." / "title"
    active: bool | None = None
    coverage: str | None = None
    evidence: list[str] = Field(default_factory=list)


class ScopusArticleCheck(BaseModel):
    status: ScopusArticleStatus = ScopusArticleStatus.UNKNOWN
    eid: str | None = None
    url: str | None = None  # Scopus record page
    citedby_count: int | None = None
    citedby_url: str | None = None  # Scopus cited-by list (required attribution link)
    source_title: str | None = None
    detail: str = ""


class ReconciliationResult(BaseModel):
    discovery_status: str
    publication_status: PublicationType
    scopus_source_status: ScopusSourceStatus
    scopus_article_status: ScopusArticleStatus
    target_version: TargetVersion
    linkage_status: LinkageStatus
    action: Action
    confidence: float
    confidence_evidence: list[Evidence]
    target_evidence: list[str]
    source_evidence: list[str]
    scopus_detail: str = ""
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)

from citation_reconciler.matching import EntityResolver, compare_records
from citation_reconciler.models import ExternalIds, PublicationType, WorkRecord


def rec(title, doi=None, year=2026, authors=("Ana Rossi",), source="openalex", **ids):
    return WorkRecord(
        ids=ExternalIds(doi=doi, **ids),
        title=title,
        year=year,
        authors=list(authors),
        sources=[source],
    )


def test_exact_doi_dedup_across_providers():
    r = EntityResolver()
    r.add(
        rec(
            "POWDR: outpainting",
            doi="10.3390/diagnostics16152385",
            source="openalex",
            openalex="W1",
        )
    )
    r.add(
        rec(
            "POWDR - Outpainting!",
            doi="10.3390/diagnostics16152385",
            source="semantic_scholar",
            s2="S1",
        )
    )
    assert len(r.clusters) == 1
    cl = r.clusters[0]
    assert cl.record.ids.openalex == "W1" and cl.record.ids.s2 == "S1"
    assert set(cl.record.sources) == {"openalex", "semantic_scholar"}
    assert cl.merge_methods == ["exact_id"]


def test_secondary_ids_bridge_clusters():
    r = EntityResolver()
    r.add(rec("Paper A", doi="10.1/a", s2="S1"))
    r.add(rec("Paper A (different title spelling)", s2="S1", source="semantic_scholar"))
    assert len(r.clusters) == 1


def test_conflicting_dois_never_merge_and_are_flagged():
    r = EntityResolver()
    r.add(rec("Deep learning for Alzheimer detection", doi="10.1/a"))
    r.add(rec("Deep learning for Alzheimer detection", doi="10.2/b"))
    assert len(r.clusters) == 2
    assert r.clusters[1].review_reasons and "conflicting DOIs" in r.clusters[1].review_reasons[0]


def test_preprint_and_journal_version_of_citing_work_merge():
    r = EntityResolver()
    r.add(
        rec(
            "Multimodal compression for Alzheimer diagnosis",
            doi="10.48550/arxiv.2601.1",
            year=2026,
            authors=("E Neri", "F Gallo"),
        )
    )
    r.add(
        rec(
            "Multimodal Compression for Alzheimer Diagnosis",
            doi="10.1016/j.x.2026.1",
            year=2026,
            authors=("Eve Neri",),
        )
    )
    assert len(r.clusters) == 1
    assert r.clusters[0].record.ids.doi == "10.1016/j.x.2026.1"  # journal DOI wins
    assert "10.48550/arxiv.2601.1" in r.clusters[0].record.extra["alt_dois"]


def test_fuzzy_title_match_requires_corroboration():
    base = rec(
        "Stage-specific hybrid ROI subset learning for MRI based Alzheimer staging", year=2026
    )
    near = rec(
        "Stage specific hybrid ROI-subset learning for MRI-based Alzheimers staging", year=2026
    )
    d = compare_records(base, near)
    assert d.matched and d.method.startswith("fuzzy")


def test_fuzzy_title_with_year_conflict_is_ambiguous():
    a = rec("Explainable machine learning models for Alzheimer diagnosis", year=2020)
    b = rec("Explainable machine learning model for Alzheimer diagnosis", year=2025)
    r = EntityResolver()
    r.add(a)
    r.add(b)
    assert len(r.clusters) == 2
    assert r.clusters[1].review_reasons  # flagged for manual review


def test_ambiguous_candidates_are_not_merged():
    r = EntityResolver()
    r.add(
        rec("Attention-based explainability for Alzheimer diagnosis", year=2025, authors=("A One",))
    )
    # Similar title (~90) but different authors: not merged, marked for review.
    r.add(
        rec(
            "Attention based explainability for Alzheimer's prognosis",
            year=2025,
            authors=("Z Two",),
        )
    )
    assert len(r.clusters) == 2
    assert any(c.review_reasons for c in r.clusters)


def test_merge_by_doi_after_resolution():
    r = EntityResolver()
    r.add(rec("Title A", doi="10.1/a"))
    c2 = r.add(rec("Totally different listing", year=2020, authors=("Q",)))
    c2.record.ids.doi = "10.1/a"  # e.g. DOI resolved later via Crossref
    assert r.merge_by_doi() == 1
    assert len(r.clusters) == 1


def test_type_rank_prefers_published_type():
    r = EntityResolver()
    a = rec("X paper", doi="10.1/x")
    a.publication_type = PublicationType.PREPRINT
    b = rec("X paper", doi="10.1/x", source="semantic_scholar")
    b.publication_type = PublicationType.JOURNAL_ARTICLE
    r.add(a)
    r.add(b)
    assert r.clusters[0].record.publication_type == PublicationType.JOURNAL_ARTICLE

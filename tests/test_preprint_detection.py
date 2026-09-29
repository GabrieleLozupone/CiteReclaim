from citation_reconciler.models import ReferenceEntry, TargetVersion, WorkRecord
from citation_reconciler.providers.crossref import parse_work
from citation_reconciler.reconciliation import classify_target

from .conftest import load_fixture


def work_with(*refs: ReferenceEntry) -> WorkRecord:
    return WorkRecord(title="Citing", references=list(refs))


def test_arxiv_doi_reference_is_preprint(paper):
    w = work_with(
        ReferenceEntry(
            doi="10.48550/arxiv.2504.08635",
            doi_asserted_by="publisher",
            author="Lozupone",
            year="2025",
            journal_title="Latent Diffusion Autoencoders ... arXiv preprint",
        )
    )
    t = classify_target(paper, w)
    assert t.target_version == TargetVersion.PREPRINT
    assert any("arXiv DOI" in e for e in t.evidence)
    assert "reference does not contain the final DOI" in t.evidence


def test_arxiv_text_with_crossref_matched_final_doi_is_preprint(paper):
    w = parse_work(load_fixture("crossref_citing_mixed.json")["message"])
    t = classify_target(paper, w)
    assert t.target_version == TargetVersion.PREPRINT
    assert "vor_crossref_matched" in t.signals
    assert any("doi-asserted-by: crossref" in e for e in t.evidence)


def test_publisher_asserted_final_doi_is_version_of_record(paper):
    w = parse_work(load_fixture("crossref_citing_vor.json")["message"])
    t = classify_target(paper, w)
    assert t.target_version == TargetVersion.VERSION_OF_RECORD
    assert "vor_explicit" in t.signals


def test_arxiv_id_plus_publisher_final_doi_is_both(paper):
    w = work_with(
        ReferenceEntry(
            doi="10.1016/j.media.2026.103932",
            doi_asserted_by="publisher",
            unstructured="Lozupone G. Latent diffusion autoencoders. arXiv:2504.08635; "
            "Med Image Anal 108 (2026) 103932",
        )
    )
    assert classify_target(paper, w).target_version == TargetVersion.BOTH


def test_journal_title_with_volume_is_version_of_record(paper):
    w = work_with(
        ReferenceEntry(
            unstructured="G. Lozupone, Latent diffusion autoencoders: toward efficient and "
            "meaningful unsupervised representation learning in medical imaging, Medical Image "
            "Analysis 108 (2026) 103932."
        )
    )
    t = classify_target(paper, w)
    assert t.target_version == TargetVersion.VERSION_OF_RECORD


def test_arxiv_url_in_reference(paper):
    w = work_with(
        ReferenceEntry(
            unstructured="Lozupone et al. Latent diffusion autoencoders. https://arxiv.org/abs/2504.08635"
        )
    )
    t = classify_target(paper, w)
    assert t.target_version == TargetVersion.PREPRINT
    assert any("arxiv.org URL" in e for e in t.evidence)


def test_year_only_weak_preprint_signal(paper):
    w = work_with(
        ReferenceEntry(
            article_title="Latent Diffusion Autoencoders: Toward Efficient and Meaningful "
            "Unsupervised Representation Learning in Medical Imaging",
            author="Lozupone",
            year="2025",
        )
    )
    t = classify_target(paper, w)
    assert t.target_version == TargetVersion.PREPRINT
    assert t.signals == ["preprint_year_only"]


def test_no_references_is_unknown(paper):
    t = classify_target(paper, WorkRecord(title="x", references=None))
    assert t.target_version == TargetVersion.UNKNOWN
    assert "no reference list" in t.evidence[0]


def test_unmatched_reference_list_is_unknown(paper):
    w = work_with(ReferenceEntry(unstructured="Totally unrelated work (2019)."))
    t = classify_target(paper, w)
    assert t.target_version == TargetVersion.UNKNOWN
    assert "none of the 1 deposited references" in t.evidence[0]


def test_discovery_via_preprint_record_is_not_evidence(paper):
    """Being found through the preprint's graph record must never imply PREPRINT."""
    paper.version_ids = {"preprint": {"openalex": "W1"}, "vor": {"openalex": "W2"}}
    w = WorkRecord(
        title="x",
        references=None,
        referenced_openalex_ids=["W1"],
        discovered_against=["openalex:W1"],
    )
    t = classify_target(paper, w)
    assert t.target_version == TargetVersion.UNKNOWN
    assert any("hint only" in e for e in t.evidence)


def test_crossref_title_whitespace_normalised():
    w = parse_work({"DOI": "10.1000/x", "title": ["Effect of\xa0the <i>KL</i>\n prior"]})
    assert w.title == "Effect of the KL prior"


def test_scopus_matched_reference_counts_as_identified(paper):
    w = work_with(
        ReferenceEntry(article_title="Latent diffusion autoencoders", source="scopus", matched=True)
    )
    t = classify_target(paper, w)
    assert "none of the" not in t.evidence[0]
    assert "scopus reference list matched" in t.evidence[0]

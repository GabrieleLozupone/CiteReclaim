import pytest

from citereclaim.models import (
    Action,
    ExternalIds,
    LinkageStatus,
    PublicationType,
    ScopusArticleCheck,
    ScopusArticleStatus,
    ScopusSourceStatus,
    SourceMatch,
    TargetClassification,
    TargetVersion,
    WorkRecord,
)
from citereclaim.reconciliation import decide_action, reconcile

P, VOR, UNK, BOTH = (
    TargetVersion.PREPRINT,
    TargetVersion.VERSION_OF_RECORD,
    TargetVersion.UNKNOWN,
    TargetVersion.BOTH,
)
J = PublicationType.JOURNAL_ARTICLE
S = ScopusSourceStatus
A = ScopusArticleStatus
L = LinkageStatus


@pytest.mark.parametrize(
    ("ptype", "target", "src", "art", "link", "expected"),
    [
        (
            PublicationType.PREPRINT,
            P,
            S.NOT_IN_LIST,
            A.UNKNOWN,
            L.NOT_APPLICABLE,
            Action.PREPRINT_ONLY,
        ),
        (J, P, S.NOT_IN_LIST, A.SCOPUS_API_UNAVAILABLE, L.UNVERIFIED, Action.NON_SCOPUS),
        (J, P, S.OUTSIDE_COVERAGE, A.SCOPUS_API_UNAVAILABLE, L.UNVERIFIED, Action.NON_SCOPUS),
        (J, P, S.COVERED, A.SCOPUS_SOURCE_ONLY, L.UNVERIFIED, Action.CHECK_SCOPUS),
        (J, UNK, S.COVERED, A.SCOPUS_SOURCE_ONLY, L.UNVERIFIED, Action.CHECK_SCOPUS),
        (J, VOR, S.COVERED, A.SCOPUS_SOURCE_ONLY, L.UNVERIFIED, Action.OK),
        (J, P, S.COVERED, A.SCOPUS_CONFIRMED, L.LINKED_TO_VOR, Action.OK),
        (J, P, S.COVERED, A.SCOPUS_CONFIRMED, L.UNVERIFIED, Action.CHECK_SCOPUS),
        (J, P, S.COVERED, A.SCOPUS_CONFIRMED, L.NOT_LINKED, Action.LIKELY_MISSING_LINK),
        (J, BOTH, S.COVERED, A.SCOPUS_CONFIRMED, L.NOT_LINKED, Action.LIKELY_MISSING_LINK),
        (J, UNK, S.NO_SOURCE_ID, A.SCOPUS_CONFIRMED, L.NOT_LINKED, Action.LIKELY_MISSING_LINK),
        (J, P, S.COVERED, A.SCOPUS_NOT_FOUND, L.NOT_APPLICABLE, Action.CHECK_SCOPUS),
        (J, P, S.NOT_IN_LIST, A.SCOPUS_NOT_FOUND, L.NOT_APPLICABLE, Action.NON_SCOPUS),
        # Permission problems never count as "not indexed".
        (J, P, S.COVERED, A.SCOPUS_PERMISSION_DENIED, L.UNVERIFIED, Action.CHECK_SCOPUS),
        (J, P, S.NO_SOURCE_ID, A.SCOPUS_PERMISSION_DENIED, L.UNVERIFIED, Action.UNKNOWN),
        (
            J,
            P,
            S.SERIES_PREVIOUSLY_INDEXED,
            A.SCOPUS_API_UNAVAILABLE,
            L.UNVERIFIED,
            Action.CHECK_SCOPUS,
        ),
        (
            PublicationType.THESIS,
            UNK,
            S.NO_SOURCE_ID,
            A.SCOPUS_API_UNAVAILABLE,
            L.UNVERIFIED,
            Action.NON_SCOPUS,
        ),
        (J, UNK, S.LIST_NOT_LOADED, A.SCOPUS_API_UNAVAILABLE, L.UNVERIFIED, Action.UNKNOWN),
    ],
)
def test_action_matrix(ptype, target, src, art, link, expected):
    assert decide_action(ptype, target, src, art, link)[0] == expected


def test_inferred_vor_is_not_auto_ok():
    action, _ = decide_action(
        J, VOR, S.COVERED, A.SCOPUS_SOURCE_ONLY, L.UNVERIFIED, vor_explicit=False
    )
    assert action == Action.CHECK_SCOPUS


def _reconcile(**kw):
    defaults = dict(
        work=WorkRecord(
            ids=ExternalIds(doi="10.1/x"),
            title="x",
            sources=["openalex", "crossref"],
            publication_type=J,
        ),
        discovered_via=["openalex", "semantic_scholar"],
        target=TargetClassification(
            target_version=P, signals=["preprint_explicit"], evidence=["reference contains arXiv:1"]
        ),
        src=SourceMatch(status=S.COVERED, matched_on="issn:1234-5678", evidence=["ISSN ok"]),
        art=ScopusArticleCheck(status=A.SCOPUS_SOURCE_ONLY),
        linkage=L.UNVERIFIED,
        identity_methods=["exact_id"],
        warnings=[],
        review_reasons=[],
    )
    defaults.update(kw)
    return reconcile(**defaults)


def test_confidence_is_explainable_and_bounded():
    r = _reconcile()
    assert 0 <= r.confidence <= 1
    signals = {e.signal for e in r.confidence_evidence}
    assert {
        "baseline",
        "doi_exact",
        "multi_source",
        "reference_cites_preprint",
        "source_id_exact",
    } <= signals
    assert r.confidence == pytest.approx(sum(e.weight for e in r.confidence_evidence), abs=0.01)


def test_scopus_confirmation_raises_confidence():
    base = _reconcile().confidence
    confirmed = _reconcile(
        art=ScopusArticleCheck(status=A.SCOPUS_CONFIRMED, eid="2-s2.0-1"), linkage=L.NOT_LINKED
    )
    assert confirmed.action == Action.LIKELY_MISSING_LINK
    assert confirmed.confidence > base


def test_penalties_for_fuzzy_and_conflicts():
    r = _reconcile(
        identity_methods=["fuzzy_title"],
        warnings=["year mismatch 2020 vs 2025", "conflicting identifiers: pmid"],
        review_reasons=["possible duplicate of X"],
    )
    signals = {e.signal: e.weight for e in r.confidence_evidence}
    assert signals["fuzzy_title_only"] < 0 and signals["year_mismatch"] < 0
    assert signals["conflicting_ids"] < 0 and signals["ambiguous_duplicate"] < 0
    assert r.needs_review
    assert r.confidence < _reconcile().confidence


def test_count_analysis_pigeonhole():
    from citereclaim.reconciliation import analyse_counts

    a = analyse_counts(citedby=4, confirmed=5, explicit_vor=1)
    assert a.deficit == 1 and not a.all_others_unlinked
    assert "at least 1 citation(s) are not linked" in a.summary
    b = analyse_counts(citedby=1, confirmed=3, explicit_vor=1)
    assert b.deficit == 2 and b.all_others_unlinked
    c = analyse_counts(citedby=6, confirmed=5, explicit_vor=0)
    assert c.deficit == 0 and "does not show missing links" in c.summary
    assert analyse_counts(None, 5, 0) is None
    assert analyse_counts(3, 0, 0) is None


def test_scopus_reference_title_in_sourcetitle(paper):
    from citereclaim.providers.scopus import ScopusReference
    from citereclaim.reconciliation import scopus_reference_link

    paper.title += " - a case study on Alzheimer's disease"  # published title is longer
    refs = [
        ScopusReference(
            "1",
            "2-s2.0-A",
            "originalReference/other",
            None,
            "Unrelated work",
            None,
            None,
            ["Smith"],
        ),
        ScopusReference(
            "110",
            "2-s2.0-ARX",
            "originalReference/other",
            None,
            "Latent Diffusion Autoencoders: Toward Efficient and Meaningful "
            "Unsupervised Representation Learning in Medical Imaging. arXiv preprint",
            None,
            None,
            ["Lozupone", "Bria"],
        ),
    ]
    link = scopus_reference_link(paper, refs, "2-s2.0-VOR")
    assert link.status == LinkageStatus.NOT_LINKED and link.linked_eid == "2-s2.0-ARX"
    assert "#110" in link.evidence[0]
    # Same title, different first author: not the tracked paper.
    refs[1].authors = ["Someone"]
    refs[1].sourcetitle = "Latent diffusion autoencoders revisited for audio"
    assert scopus_reference_link(paper, refs, "2-s2.0-VOR").status == LinkageStatus.UNVERIFIED

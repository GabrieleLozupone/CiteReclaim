import pytest

from citereclaim.matching import (
    find_arxiv_ids,
    find_dois,
    is_preprint_doi,
    normalize_arxiv_id,
    normalize_doi,
    normalize_isbn,
    normalize_issn,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1016/J.MEDIA.2026.103932", "10.1016/j.media.2026.103932"),
        ("https://doi.org/10.1016/j.media.2026.103932", "10.1016/j.media.2026.103932"),
        ("http://dx.doi.org/10.1186/s12911-026-03833-2", "10.1186/s12911-026-03833-2"),
        ("doi:10.1186/s12911-026-03833-2", "10.1186/s12911-026-03833-2"),
        ("DOI: 10.1186/s12911-026-03833-2.", "10.1186/s12911-026-03833-2"),
        ("  10.48550/arXiv.2504.08635 ;", "10.48550/arxiv.2504.08635"),
        ("https://doi.org/doi:10.1000/xyz)", "10.1000/xyz"),
        ("not a doi", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_doi(raw, expected):
    assert normalize_doi(raw) == expected


def test_find_dois_in_text():
    text = "See https://doi.org/10.1016/j.media.2026.103932, and doi:10.48550/arXiv.2504.08635."
    assert find_dois(text) == ["10.1016/j.media.2026.103932", "10.48550/arxiv.2504.08635"]


def test_preprint_doi_prefixes():
    assert is_preprint_doi("10.48550/arXiv.2504.08635")
    assert is_preprint_doi("10.1101/2024.01.01.123456")
    assert not is_preprint_doi("10.1016/j.media.2026.103932")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2504.08635", "2504.08635"),
        ("arXiv:2504.08635v3", "2504.08635"),
        ("https://arxiv.org/abs/2407.02418v1", "2407.02418"),
        ("https://arxiv.org/pdf/2407.02418.pdf", "2407.02418"),
        ("10.48550/arXiv.2407.02418", "2407.02418"),
        ("hep-th/9901001", "hep-th/9901001"),
        ("12345", None),
    ],
)
def test_normalize_arxiv(raw, expected):
    assert normalize_arxiv_id(raw) == expected


def test_find_arxiv_ids_requires_context():
    assert find_arxiv_ids("arXiv preprint arXiv:2504.08635 (2025)") == ["2504.08635"]
    assert find_arxiv_ids("https://arxiv.org/abs/2407.02418") == ["2407.02418"]
    # A page range must not be mistaken for an arXiv id.
    assert find_arxiv_ids("Med Image Anal 108, 1234.5678 (2026)") == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1361-8415", "1361-8415"),
        ("13618415", "1361-8415"),
        ("1433-755x", "1433-755X"),
        (" 2075 4418 ", "2075-4418"),
        ("2349130", "0234-9130"),  # leading zero dropped by a spreadsheet
        ("abc", None),
        (None, None),
    ],
)
def test_normalize_issn(raw, expected):
    assert normalize_issn(raw) == expected


def test_normalize_isbn():
    assert normalize_isbn("978-1-6654-7494-8") == "9781665474948"
    assert normalize_isbn(9781665474948) == "9781665474948"
    assert normalize_isbn("123") is None

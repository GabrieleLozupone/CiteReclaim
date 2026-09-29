from citation_reconciler.providers.scholar_import import (
    parse_bibtex_text,
    parse_file,
    row_hash,
    split_authors,
)

from .conftest import FIXTURES


def test_csv_flexible_columns():
    rows = parse_file(FIXTURES / "scholar_citations.csv")
    assert len(rows) == 3
    assert rows[0]["title"].startswith("POWDR")
    assert rows[0]["journal"] == "Diagnostics"  # "Source" column alias
    assert rows[0]["year"] == 2026
    assert rows[0]["doi"] is None  # missing DOI is expected
    # DOI recovered from a doi.org URL
    assert rows[2]["doi"] == "10.1007/s10044-026-01765-1"


def test_csv_semicolon_and_lowercase_headers(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text(
        "title;authors;year;journal;doi;url\nA paper;X, Y;2024;J;doi:10.1000/ABC;\n",
        encoding="utf-8",
    )
    rows = parse_file(p)
    assert rows == [
        {
            "title": "A paper",
            "authors": "X, Y",
            "year": 2024,
            "journal": "J",
            "doi": "10.1000/abc",
            "arxiv": None,
        }
    ]


def test_bibtex_import():
    rows = parse_file(FIXTURES / "scholar_citations.bib")
    assert len(rows) == 2
    assert (
        rows[0]["title"] == "POWDR: Pathology-Preserving Outpainting with Wavelet Diffusion "
        "for 3D MRI"
    )
    assert rows[0]["doi"] == "10.3390/diagnostics16152385"
    assert rows[1]["journal"].startswith("arXiv preprint")
    assert rows[1]["arxiv"] == "2601.14584"
    assert split_authors(rows[0]["authors"]) == ["Rossi, Ana", "Chen, Ben"]


def test_bibtex_nested_braces_and_bare_values():
    rows = parse_bibtex_text("@misc{k, title={A {B} C}, year=2020, note={x}}")
    assert rows[0]["title"] == "A B C" and rows[0]["year"] == 2020


def test_json_import_csl_and_plain():
    rows = parse_file(FIXTURES / "scholar_citations.json")
    assert rows[0]["doi"] == "10.3390/diagnostics16152385"
    assert rows[0]["authors"] == "Ana Rossi"
    assert rows[0]["year"] == 2026 and rows[0]["journal"] == "Diagnostics"
    assert rows[1]["title"] == "Some Thesis" and rows[1]["journal"] == "University of X"


def test_row_hash_stable():
    r = {"title": "A", "doi": None, "year": 2020, "journal": "J", "extra": 1}
    assert row_hash(r) == row_hash({**r, "extra": 2})


def test_split_authors_variants():
    assert split_authors("A Rossi, B Chen, …") == ["A Rossi", "B Chen"]
    assert split_authors("Rossi, A and Chen, B") == ["Rossi, A", "Chen, B"]
    assert split_authors(None) == []


def test_bibtex_latex_accents():
    rows = parse_bibtex_text(
        "@mastersthesis{k, title={Detecci{\\'o}n de Im{\\'a}genes}, author={Fl{\\'o}rez, A},"
        " journal={SGS-Engineering \\& Sciences}, year={2026}}"
    )
    assert rows[0]["title"] == "Detección de Imágenes"
    assert rows[0]["authors"] == "Flórez, A"
    assert rows[0]["journal"] == "SGS-Engineering & Sciences"


def test_bibtex_dotless_i_and_umlaut():
    rows = parse_bibtex_text(
        "@article{k, title={X}, author={Mart{\\'\\i}nez-Abad{\\'\\i}as, Neus and T{\\\"u}mer, N},"
        " year={2026}}"
    )
    assert rows[0]["authors"] == "Martínez-Abadías, Neus and Tümer, N"

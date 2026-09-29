import json

from typer.testing import CliRunner

from citereclaim.cli import app

runner = CliRunner()


def invoke(tmp_path, *args):
    return runner.invoke(app, ["--home", str(tmp_path / "h"), *args], env={"CITERECLAIM_HOME": ""})


def test_help():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0 and "scopus-sources" in r.output


def test_init_and_paper_add_list(tmp_path):
    assert invoke(tmp_path, "init").exit_code == 0
    r = invoke(
        tmp_path,
        "paper",
        "add",
        "--name",
        "LDAE",
        "--arxiv",
        "arXiv:2504.08635v2",
        "--doi",
        "https://doi.org/10.1016/J.MEDIA.2026.103932",
    )
    assert r.exit_code == 0, r.output
    r = invoke(tmp_path, "paper", "list", "--json")
    data = json.loads(r.output)
    assert data[0]["arxiv_id"] == "2504.08635"
    assert data[0]["arxiv_doi"] == "10.48550/arxiv.2504.08635"
    assert data[0]["journal_doi"] == "10.1016/j.media.2026.103932"


def test_paper_add_validation(tmp_path):
    r = invoke(tmp_path, "paper", "add", "--name", "X", "--doi", "nope")
    assert r.exit_code == 2
    r = invoke(tmp_path, "paper", "add", "--name", "X")
    assert r.exit_code == 2


def test_paper_import_yaml(tmp_path):
    r = invoke(tmp_path, "paper", "import", "examples/papers.yaml")
    assert r.exit_code == 0, r.output
    data = json.loads(invoke(tmp_path, "paper", "list", "--json").output)
    assert {p["name"] for p in data} == {"LDAE", "AXIAL"}


def test_report_empty_and_unknown_paper(tmp_path):
    invoke(tmp_path, "paper", "add", "--name", "A", "--arxiv", "2407.02418")
    r = invoke(tmp_path, "report", "A", "--json")
    assert r.exit_code == 0 and json.loads(r.output)["summary"]["unique_citing_works"] == 0
    assert invoke(tmp_path, "report", "NOPE").exit_code == 2


def test_scopus_sources_status_when_empty(tmp_path):
    r = invoke(tmp_path, "scopus-sources", "status")
    assert r.exit_code == 1 and "No Scopus Source List" in r.output


def test_scholar_import_requires_paper_choice(tmp_path):
    invoke(tmp_path, "paper", "add", "--name", "A", "--arxiv", "2407.02418")
    invoke(tmp_path, "paper", "add", "--name", "B", "--arxiv", "2504.08635")
    r = invoke(tmp_path, "scholar", "import", "tests/fixtures/scholar_citations.csv")
    assert r.exit_code == 2
    r = invoke(
        tmp_path,
        "scholar",
        "import",
        "tests/fixtures/scholar_citations.csv",
        "--paper",
        "A",
        "--no-sync",
    )
    assert r.exit_code == 0 and "Parsed 3 rows" in r.output


def test_export_empty(tmp_path):
    invoke(tmp_path, "paper", "add", "--name", "A", "--arxiv", "2407.02418")
    out = tmp_path / "r.csv"
    assert invoke(tmp_path, "export", "--format", "csv", "--output", str(out)).exit_code == 0
    assert out.exists()
    r = invoke(tmp_path, "export-support", "A", "--output-dir", str(tmp_path / "o"))
    assert r.exit_code == 0
    for f in (
        "scopus_missing_citations.md",
        "scopus_reference_linking.xlsx",
        "scopus_support_request.txt",
    ):
        assert (tmp_path / "o" / "A" / f).exists()

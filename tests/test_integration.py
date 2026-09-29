"""Live API tests. Not run by default: `pytest -m integration`."""

import pytest

from citation_reconciler.config import load_settings
from citation_reconciler.db import Database
from citation_reconciler.providers.crossref import CrossrefClient
from citation_reconciler.providers.openalex import OpenAlexClient
from citation_reconciler.providers.scopus_sources import CONTENT_PAGE, discover_download_url
from citation_reconciler.providers.semantic_scholar import SemanticScholarClient

pytestmark = pytest.mark.integration


@pytest.fixture
def live(tmp_path):
    s = load_settings(home=tmp_path)
    s.ensure_dirs()
    db = Database(s.db_path)
    yield s, db
    db.close()


def test_crossref_live(live):
    rec = CrossrefClient(*live).get_work("10.1038/nature14539")
    assert rec and rec.title and rec.issns


def test_openalex_live(live):
    rec = OpenAlexClient(*live).get_work("10.1038/nature14539")
    assert rec and rec.ids.openalex


def test_semantic_scholar_live(live):
    rec = SemanticScholarClient(*live).get_by_arxiv("1706.03762")
    assert rec and rec.ids.s2


def test_scopus_source_list_discovery_live():
    import httpx

    page = httpx.get(
        CONTENT_PAGE,
        headers={"User-Agent": "citation-reconciler-tests"},
        follow_redirects=True,
        timeout=60,
    )
    url, _ = discover_download_url(page.text)
    assert url.endswith(".xlsx")

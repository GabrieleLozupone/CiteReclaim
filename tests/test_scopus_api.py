import httpx
import respx

from citereclaim.models import ScopusArticleStatus
from citereclaim.providers.scopus import SEARCH_URL, ScopusClient


def scopus(settings, db):
    settings.elsevier_api_key = "KEY"
    return ScopusClient(settings, db, sleep=lambda s: None)


def found(eid="2-s2.0-1", doi="10.1000/x"):
    return {
        "search-results": {
            "opensearch:totalResults": "1",
            "entry": [
                {
                    "eid": eid,
                    "prism:doi": doi,
                    "dc:title": "T",
                    "prism:publicationName": "J",
                    "prism:coverDate": "2026-01-01",
                    "subtype": "ar",
                }
            ],
        }
    }


EMPTY = {
    "search-results": {
        "opensearch:totalResults": "0",
        "entry": [{"@_fa": "true", "error": "Result set was empty"}],
    }
}


def test_no_key_is_unavailable_not_not_indexed(settings, db):
    c = ScopusClient(settings, db)
    assert not c.enabled
    assert c.lookup_doi("10.1000/x").status == ScopusArticleStatus.SCOPUS_API_UNAVAILABLE


@respx.mock
def test_confirmed_and_headers(settings, db):
    settings.elsevier_insttoken = "TOK"
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=found()))
    chk = scopus(settings, db).lookup_doi("https://doi.org/10.1000/X")
    assert chk.status == ScopusArticleStatus.SCOPUS_CONFIRMED and chk.eid == "2-s2.0-1"
    req = route.calls[0].request
    assert req.headers["X-ELS-APIKey"] == "KEY" and req.headers["X-ELS-Insttoken"] == "TOK"
    assert 'DOI("10.1000/x")' in req.url.params["query"]


@respx.mock
def test_not_found(settings, db):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=EMPTY))
    assert (
        scopus(settings, db).lookup_doi("10.1000/x").status == ScopusArticleStatus.SCOPUS_NOT_FOUND
    )


@respx.mock
def test_403_is_permission_denied_and_stops_querying(settings, db):
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            403, json={"service-error": {"status": {"statusCode": "AUTHORIZATION_ERROR"}}}
        )
    )
    c = scopus(settings, db)
    assert c.lookup_doi("10.1000/x").status == ScopusArticleStatus.SCOPUS_PERMISSION_DENIED
    assert c.lookup_doi("10.1000/y").status == ScopusArticleStatus.SCOPUS_PERMISSION_DENIED
    assert c.citing_documents("2-s2.0-9").status == ScopusArticleStatus.SCOPUS_PERMISSION_DENIED
    assert route.call_count == 1  # never hammered after a 403


@respx.mock
def test_401_is_permission_denied(settings, db):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(401))
    assert (
        scopus(settings, db).lookup_doi("10.1000/x").status
        == ScopusArticleStatus.SCOPUS_PERMISSION_DENIED
    )


@respx.mock
def test_quota_exhausted_is_unavailable(settings, db):
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "999999"}))
    c = scopus(settings, db)
    assert c.lookup_doi("10.1000/x").status == ScopusArticleStatus.SCOPUS_API_UNAVAILABLE
    assert c.lookup_doi("10.1000/y").status == ScopusArticleStatus.SCOPUS_API_UNAVAILABLE


@respx.mock
def test_citing_documents_paging(settings, db):
    page1 = {
        "search-results": {
            "opensearch:totalResults": "2",
            "entry": [found("2-s2.0-A", "10.1000/a")["search-results"]["entry"][0]],
        }
    }
    page2 = {
        "search-results": {
            "opensearch:totalResults": "2",
            "entry": [found("2-s2.0-B", "10.1000/b")["search-results"]["entry"][0]],
        }
    }
    respx.get(SEARCH_URL).mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    res = scopus(settings, db).citing_documents("2-s2.0-VOR")
    assert (
        res.ok and res.eids == {"2-s2.0-A", "2-s2.0-B"} and res.dois == {"10.1000/a", "10.1000/b"}
    )


@respx.mock
def test_refeid_not_entitled_keeps_doi_lookups_working(settings, db):
    restricted = {
        "service-error": {
            "status": {
                "statusCode": "INVALID_INPUT",
                "statusText": "Use of certain field restrictions in the search query is "
                "not allowed for this requestor.",
            }
        }
    }

    def handler(request):
        if request.url.params["query"].startswith("REFEID"):
            return httpx.Response(400, json=restricted)
        return httpx.Response(200, json=found())

    respx.get(SEARCH_URL).mock(side_effect=handler)
    c = scopus(settings, db)
    res = c.citing_documents("2-s2.0-VOR")
    assert not res.ok and res.status == ScopusArticleStatus.SCOPUS_PERMISSION_DENIED
    assert "REFEID() is not permitted" in res.detail
    # Article-level lookups are still allowed with the same key.
    assert c.lookup_doi("10.1000/x").status == ScopusArticleStatus.SCOPUS_CONFIRMED


@respx.mock
def test_reference_list_paging_respects_total(settings, db):
    url = "https://api.elsevier.com/content/abstract/eid/2-s2.0-1"

    def page(n0, n1, total):
        refs = [
            {
                "@id": str(i),
                "scopus-eid": f"2-s2.0-R{i}",
                "title": f"T{i}",
                "ce:doi": [f"10.1000/r{i}", "10.1000/dup"],
            }
            for i in range(n0, n1 + 1)
        ]
        return {
            "abstracts-retrieval-response": {
                "references": {"@total-references": str(total), "reference": refs}
            }
        }

    def handler(request):
        p = request.url.params
        if "startref" not in p:
            return httpx.Response(200, json=page(1, 40, 45))
        assert p["startref"] == "41" and "refcount" not in p
        return httpx.Response(200, json=page(41, 45, 45))

    respx.get(url).mock(side_effect=handler)
    refs, why = scopus(settings, db).references("2-s2.0-1")
    assert why == "ok" and len(refs) == 45 and refs[-1].eid == "2-s2.0-R45"
    assert refs[0].doi == "10.1000/r1"  # list-valued DOI handled


@respx.mock
def test_reference_list_denied_keeps_search_working(settings, db):
    respx.get("https://api.elsevier.com/content/abstract/eid/2-s2.0-1").mock(
        return_value=httpx.Response(401)
    )
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=found()))
    c = scopus(settings, db)
    refs, why = c.references("2-s2.0-1")
    assert refs is None and "view=REF" in why
    assert c.lookup_doi("10.1000/x").status == ScopusArticleStatus.SCOPUS_CONFIRMED

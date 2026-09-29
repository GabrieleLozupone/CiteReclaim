import httpx
import pytest
import respx

from citation_reconciler.providers.base import (
    HttpClient,
    PermissionDenied,
    ProviderUnavailable,
    RateLimited,
    RateLimiter,
    cache_key,
    parse_retry_after,
)

URL = "https://api.example.org/thing"


def client(settings, db, sleeps, **kw):
    kw.setdefault("max_retries", 3)
    return HttpClient("example", settings, db, min_interval=0.0, sleep=sleeps.append, **kw)


def test_rate_limiter_enforces_min_interval():
    t = [100.0]
    slept: list[float] = []

    def sleep(x):
        slept.append(x)
        t[0] += x

    rl = RateLimiter(1.0, jitter=0.0, clock=lambda: t[0], sleep=sleep)
    rl.wait()
    rl.wait()
    rl.wait()
    assert slept == [pytest.approx(1.0), pytest.approx(1.0)]


def test_parse_retry_after():
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412470.0) == pytest.approx(
        10.0
    )
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None


@respx.mock
def test_cache_hit_avoids_second_request(settings, db):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"a": 1}))
    c = client(settings, db, [])
    assert c.get(URL, {"q": "x"}, ttl=60).json() == {"a": 1}
    r2 = c.get(URL, {"q": "x"}, ttl=60)
    assert r2.from_cache and r2.json() == {"a": 1}
    assert route.call_count == 1
    assert c.cache_hits == 1


@respx.mock
def test_expired_cache_refetches_and_refresh_bypasses(settings, db):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"a": 1}))
    c = client(settings, db, [])
    c.get(URL, ttl=-1)  # already expired
    c.get(URL, ttl=60)
    assert route.call_count == 2
    settings.refresh = True
    c.get(URL, ttl=60)
    assert route.call_count == 3


@respx.mock
def test_cache_key_ignores_secrets(settings, db):
    assert cache_key("p", "GET", URL, {"q": 1, "api_key": "A"}) == cache_key(
        "p", "GET", URL, {"q": 1, "api_key": "B"}
    )
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    c = client(settings, db, [])
    c.get(URL, {"q": 1, "api_key": "A"}, ttl=60)
    c.get(URL, {"q": 1, "api_key": "B"}, ttl=60)
    assert route.call_count == 1
    row = db.conn.execute("SELECT url FROM api_cache").fetchone()
    assert row is not None  # cached url may include key for debugging, key is not in cache key


@respx.mock
def test_retry_after_is_honoured_on_429(settings, db):
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    sleeps: list[float] = []
    c = client(settings, db, sleeps)
    assert c.get(URL, ttl=60).json() == {"ok": True}
    assert route.call_count == 2
    assert any(s == pytest.approx(3, abs=0.5) for s in sleeps)


@respx.mock
def test_429_exhaustion_raises_rate_limited(settings, db):
    route = respx.get(URL).mock(return_value=httpx.Response(429))
    sleeps: list[float] = []
    c = client(settings, db, sleeps, max_retries=2, backoff_base=1.0)
    with pytest.raises(RateLimited):
        c.get(URL, ttl=60)
    assert route.call_count == 3
    # exponential backoff: roughly 1, 2 (+ jitter)
    assert sleeps[0] >= 1.0 and sleeps[1] >= 2.0


@respx.mock
def test_huge_retry_after_gives_up_immediately(settings, db):
    route = respx.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "86400"}))
    with pytest.raises(RateLimited):
        client(settings, db, []).get(URL, ttl=60)
    assert route.call_count == 1


@respx.mock
def test_403_is_never_retried(settings, db):
    route = respx.get(URL).mock(return_value=httpx.Response(403, text="forbidden"))
    with pytest.raises(PermissionDenied) as exc:
        client(settings, db, []).get(URL, ttl=60)
    assert route.call_count == 1 and exc.value.status == 403
    assert db.conn.execute("SELECT COUNT(*) FROM api_cache").fetchone()[0] == 0


@respx.mock
def test_5xx_then_success(settings, db):
    respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, json=[1])])
    assert client(settings, db, []).get(URL, ttl=60).json() == [1]


@respx.mock
def test_transport_error_becomes_unavailable(settings, db):
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderUnavailable):
        client(settings, db, [], max_retries=1).get(URL, ttl=60)


@respx.mock
def test_404_cached_with_short_ttl(settings, db):
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    c = client(settings, db, [])
    assert c.get(URL, ttl=10**9).status == 404
    assert c.get(URL, ttl=10**9).from_cache
    assert route.call_count == 1
    exp = db.conn.execute("SELECT expires_at - fetched_at FROM api_cache").fetchone()[0]
    assert exp == pytest.approx(settings.ttl.not_found)


def test_offline_mode_without_cache(settings, db):
    settings.offline = True
    with pytest.raises(ProviderUnavailable):
        client(settings, db, []).get(URL, ttl=60)


def test_user_agent_identifies_tool(settings):
    settings.crossref_mailto = "me@example.org"
    ua = settings.user_agent()
    assert ua.startswith("citation-reconciler/") and "mailto:me@example.org" in ua


@respx.mock
def test_crossref_search_client_error_is_reported(settings, db):
    from citation_reconciler.providers.base import ProviderError
    from citation_reconciler.providers.crossref import CrossrefClient

    respx.get("https://api.crossref.org/works").mock(return_value=httpx.Response(400))
    with pytest.raises(ProviderError):
        CrossrefClient(settings, db, sleep=lambda s: None).search_bibliographic("x")

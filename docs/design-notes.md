# Design notes — CiteReclaim

Background research on the external services and the module layout. The user-facing
documentation is the [README](../README.md).

## Findings from current documentation (checked 2026-09-29)

| Service | Finding | Consequence |
|---|---|---|
| Crossref REST | No signup. Public pool: 5 req/interval, concurrency 1. Polite pool (`mailto` param or in User-Agent): 10 req/interval, concurrency 3. 429 = rate-limited, 403 = manual block. | `CROSSREF_MAILTO` optional but recommended; limiter 0.25 s (public) / 0.12 s (polite); never hammer on 403. |
| Crossref references | Deposited references include `DOI`, `doi-asserted-by` (`publisher`/`crossref`), `unstructured`, `journal-title`, `year`. Crossref's matcher sometimes attaches the VoR DOI to a reference whose text says "arXiv". | Primary signal for target-version classification. Distinguish publisher-asserted vs Crossref-matched DOIs. |
| Semantic Scholar | Most endpoints public without auth; anonymous traffic shares a pool and is throttled (429 observed in practice). Key: introductory 1 RPS. Header `x-api-key`. S2 merges arXiv + journal versions into one paper. | Key optional; anonymous limiter ≥1.1 s + aggressive backoff; never use S2 alone to infer preprint target. |
| OpenAlex | Since Feb 2026: usage-based pricing. Singleton lookups (by ID/DOI) free; list+filter $0.10/1000; search $1/1000. Free key = $1/day; no key = $0.10/day (testing). Key passed as `api_key` query param. Preprint and VoR are sometimes separate works, sometimes merged, sometimes one is missing. | `OPENALEX_API_KEY` optional but strongly recommended; `OPENALEX_MAILTO` still sent (harmless); prefer DOI singleton lookups; handle budget-exhausted 429 as "unavailable" not fatal. |
| Scopus APIs | Free for non-commercial use by eligible academic users. Search: 20k/week, 9 req/s; Abstract Retrieval: 10k/week. Citation Overview requires special approval. `X-ELS-APIKey`, `X-ELS-Insttoken`. Unauthenticated → 401. | Fully optional. Use Scopus Search `DOI()` for article check and `REFEID()` for cited-by comparison. 401/403 → `SCOPUS_PERMISSION_DENIED`, never "not indexed". |
| Scopus Source Title List | Linked from https://www.elsevier.com/products/scopus/content as a Contentful asset (`ext_list_<Mon>_<YYYY>.xlsx`, ~26 MB, monthly). robots.txt allows. Sheets: main sources (ISSN/EISSN/Coverage/Active), Accepted, Discontinued, Serial Conf. Proc., All Conf. Proceedings (ISBN). | Auto-discovery by parsing the page for the "Source title list" asset + manual `import` (xlsx/csv). Header-detection parsing, not fixed column positions. |
| Google Scholar | No official API. | Import only (CSV/BibTeX/JSON). |

## Architecture

- `providers/base.py` — `HttpClient`: httpx + per-provider min-interval limiter with jitter, exponential backoff, `Retry-After`, SQLite response cache with TTLs, typed errors (`PermissionDenied`, `RateLimited`, `ProviderUnavailable`). No retry on 401/403/404.
- `providers/{crossref,semantic_scholar,openalex,scopus}.py` — thin adapters returning `WorkRecord`s.
- `providers/scopus_sources.py` — discovery, download, parsing, local index (ISSN, ISBN, normalised title).
- `providers/scholar_import.py` — CSV / BibTeX / JSON parsers with flexible column aliases.
- `matching.py` — DOI/arXiv/ISSN/title normalisation, fuzzy matching, entity resolution with manual-review flags.
- `reconciliation.py` — target-version classifier, Scopus source matching, action logic, confidence model.
- `pipeline.py` — sync orchestration (resolve versions → discover → dedupe → enrich → classify → reconcile → store).
- `db.py` — sqlite3 schema + repository functions.
- `reporting.py` — Rich tables, summary, CSV/JSON export, Scopus-support evidence pack.
- `cli.py` — Typer app.


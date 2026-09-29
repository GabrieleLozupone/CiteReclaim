"""Command-line interface (Typer + Rich)."""

from __future__ import annotations

import json
import logging
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import Settings, load_settings
from .db import Database
from .matching import arxiv_doi, normalize_arxiv_id, normalize_doi, normalize_issn
from .models import TrackedPaper
from .pipeline import Providers, Syncer
from .providers import scholar_import, scopus_sources
from .providers.base import ProviderError
from .reporting import dump_json, export_rows, export_support, render_report, report_json, write_csv

console = Console()
err = Console(stderr=True)

app = typer.Typer(
    name="citereclaim",
    help="Discover citations to preprint and published versions of the same work and flag "
    "citations that may need reconciliation in Scopus.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)
paper_app = typer.Typer(help="Manage tracked papers.", no_args_is_help=True)
scholar_app = typer.Typer(
    help="Import Google Scholar 'Cited by' exports (no scraping).", no_args_is_help=True
)
sources_app = typer.Typer(
    help="Manage the local copy of the Scopus Source Title List.", no_args_is_help=True
)
cache_app = typer.Typer(help="Inspect or clear the HTTP response cache.", no_args_is_help=True)
citation_app = typer.Typer(
    help="Add citations you verified yourself (e.g. seen in Scopus) that the open indexes miss.",
    no_args_is_help=True,
)
app.add_typer(paper_app, name="paper")
app.add_typer(scholar_app, name="scholar")
app.add_typer(sources_app, name="scopus-sources")
app.add_typer(cache_app, name="cache")
app.add_typer(citation_app, name="citation")


class State:
    settings: Settings
    _db: Database | None = None

    @property
    def db(self) -> Database:
        if self._db is None:
            self.settings.ensure_dirs()
            self._db = Database(self.settings.db_path)
        return self._db


state = State()


def _version(value: bool) -> None:
    if value:
        console.print(f"citereclaim {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    home: Annotated[
        Path | None,
        typer.Option(
            "--home",
            help="Data directory (default: $CITERECLAIM_HOME or ~/.citereclaim).",
            envvar="CITERECLAIM_HOME",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose logging.")] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version and exit."),
    ] = False,
) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    state.settings = load_settings(home=home.expanduser() if home else None)


def _paper_or_exit(name: str) -> TrackedPaper:
    p = state.db.get_paper(name)
    if not p:
        err.print(f"[red]Unknown paper '{name}'.[/red] Use `citereclaim paper list`.")
        raise typer.Exit(2)
    return p


def _papers(name: str | None, all_: bool) -> list[TrackedPaper]:
    if all_:
        papers = state.db.list_papers()
        if not papers:
            err.print("[yellow]No papers tracked yet.[/yellow] Add one with `paper add`.")
            raise typer.Exit(1)
        return papers
    if not name:
        err.print("[red]Give a paper name or --all.[/red]")
        raise typer.Exit(2)
    return [_paper_or_exit(name)]


# ============================================================================ init / doctor


@app.command()
def init() -> None:
    """Create the data directory and local database."""
    s = state.settings
    s.ensure_dirs()
    _ = state.db
    console.print(f"[green]Initialised[/green] {s.db_path}")
    _print_config()
    st = scopus_sources.status(state.db)
    if not st:
        console.print(
            "\nNext: [b]citereclaim scopus-sources update[/b] "
            "(downloads the free Scopus Source Title List, ~25 MB)"
        )


def _print_config() -> None:
    s = state.settings
    t = Table(title="Configuration", show_header=True, header_style="bold")
    t.add_column("Setting")
    t.add_column("Status")
    t.add_column("Notes", overflow="fold")

    def mark(v: object) -> str:
        return "[green]set[/green]" if v else "[dim]not set[/dim]"

    t.add_row(
        "CROSSREF_MAILTO",
        mark(s.crossref_mailto),
        "polite pool" if s.crossref_mailto else "public pool (slower); recommended",
    )
    t.add_row(
        "SEMANTIC_SCHOLAR_API_KEY",
        mark(s.semantic_scholar_api_key),
        "optional; anonymous requests are throttled",
    )
    t.add_row(
        "OPENALEX_API_KEY",
        mark(s.openalex_api_key),
        "optional; without it OpenAlex allows only a small daily test budget",
    )
    t.add_row("OPENALEX_MAILTO", mark(s.openalex_mailto), "optional")
    t.add_row(
        "ELSEVIER_API_KEY",
        mark(s.elsevier_api_key),
        "optional; enables article-level Scopus verification",
    )
    t.add_row(
        "ELSEVIER_INSTTOKEN",
        mark(s.elsevier_insttoken),
        "optional; needed for off-campus Scopus access",
    )
    t.add_row("Data directory", str(s.home), "")
    console.print(t)


@app.command()
def doctor(
    offline: Annotated[bool, typer.Option(help="Skip network checks.")] = False,
) -> None:
    """Check configuration, database, Scopus Source List and provider connectivity."""
    _print_config()
    db = state.db
    papers = db.list_papers()
    console.print(f"Database: {state.settings.db_path} ({len(papers)} tracked papers)")
    st = scopus_sources.status(db)
    if st:
        warn = " [yellow](older than 30 days; run update)[/yellow]" if st["age_days"] > 30 else ""
        console.print(
            f"Scopus Source List: {st['version_label'] or '?'} — {st['n_sources']} "
            f"sources, imported {st['imported_at']} ({st['age_days']} d ago){warn}"
        )
    else:
        console.print(
            "Scopus Source List: [red]not loaded[/red] — run `citereclaim scopus-sources update`"
        )
    if offline:
        return
    s = state.settings
    s.refresh = True  # connectivity checks must hit the network
    providers = Providers.build(s, db, max_retries=1)
    checks = [
        ("Crossref", lambda: providers.crossref.get_work("10.1038/nature14539")),
        ("Semantic Scholar", lambda: providers.s2.get_by_arxiv("1706.03762")),
        ("OpenAlex", lambda: providers.openalex.get_work("10.1038/nature14539")),
    ]
    t = Table(title="Connectivity", header_style="bold")
    t.add_column("Provider")
    t.add_column("Result", overflow="fold")
    for name, fn in checks:
        try:
            rec = fn()
            t.add_row(name, "[green]ok[/green]" if rec else "[yellow]reachable, no record[/yellow]")
        except ProviderError as exc:
            t.add_row(name, f"[red]{type(exc).__name__}[/red]: {exc}")
    caps: list[tuple[str, bool, str]] = []
    if s.scopus_api_configured:
        status, detail = providers.scopus.probe()
        style = "green" if status.value in ("SCOPUS_CONFIRMED", "SCOPUS_NOT_FOUND") else "red"
        t.add_row("Scopus API", f"[{style}]{status.value}[/{style}] {detail}")
        caps = providers.scopus.capabilities()
    else:
        t.add_row("Scopus API", "[dim]not configured (optional)[/dim]")
    providers.close()
    console.print(t)
    if caps:
        ct = Table(title="Scopus entitlement (this key, this network)", header_style="bold")
        ct.add_column("Feature")
        ct.add_column("Allowed")
        ct.add_column("Detail", overflow="fold")
        for name, ok, detail in caps:
            ct.add_row(name, "[green]yes[/green]" if ok else "[red]no[/red]", detail)
        console.print(ct)
        if not all(ok for _, ok, _ in caps):
            console.print(
                "Blocked features usually need institutional entitlement: run from your "
                "institution's network/VPN, or set ELSEVIER_INSTTOKEN (requested from Elsevier's "
                "Research Product APIs Support Center)."
            )


# ============================================================================ papers


@paper_app.command("add")
def paper_add(
    name: Annotated[str, typer.Option("--name", help="Short unique name, e.g. LDAE.")],
    arxiv: Annotated[str | None, typer.Option("--arxiv", help="arXiv id, e.g. 2504.08635.")] = None,
    doi: Annotated[str | None, typer.Option("--doi", help="Final (journal) DOI.")] = None,
    arxiv_doi_opt: Annotated[
        str | None,
        typer.Option("--arxiv-doi", help="Preprint DOI (defaults to 10.48550/arXiv.<id>)."),
    ] = None,
    title: Annotated[str | None, typer.Option("--title")] = None,
    authors: Annotated[
        str | None, typer.Option("--authors", help="Semicolon-separated author names.")
    ] = None,
) -> None:
    """Track a paper (preprint and/or Version of Record)."""
    aid = normalize_arxiv_id(arxiv) if arxiv else None
    if arxiv and not aid:
        err.print(f"[red]Not a valid arXiv id: {arxiv}[/red]")
        raise typer.Exit(2)
    jdoi = normalize_doi(doi) if doi else None
    if doi and not jdoi:
        err.print(f"[red]Not a valid DOI: {doi}[/red]")
        raise typer.Exit(2)
    if not (aid or jdoi or arxiv_doi_opt):
        err.print("[red]Give at least --arxiv or --doi.[/red]")
        raise typer.Exit(2)
    existing = state.db.get_paper(name)
    p = existing or TrackedPaper(name=name)
    p.arxiv_id = aid or p.arxiv_id
    p.arxiv_doi = (
        normalize_doi(arxiv_doi_opt)
        if arxiv_doi_opt
        else (arxiv_doi(p.arxiv_id) if p.arxiv_id else p.arxiv_doi)
    )
    p.journal_doi = jdoi or p.journal_doi
    p.title = title or p.title
    if authors:
        p.authors = [a.strip() for a in authors.split(";") if a.strip()]
    p = state.db.upsert_paper(p)
    verb = "Updated" if existing else "Added"
    console.print(
        f"[green]{verb}[/green] {p.name}: arXiv {p.arxiv_id or '-'} "
        f"(preprint DOI {p.arxiv_doi or '-'}), final DOI {p.journal_doi or '-'}"
    )
    console.print(f"Next: citereclaim sync {p.name}")


@paper_app.command("import")
def paper_import(file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Add papers from a YAML file (see examples/papers.yaml)."""
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    for item in data.get("papers") or []:
        aid = normalize_arxiv_id(str(item.get("arxiv_id") or "")) or None
        p = state.db.get_paper(item["name"]) or TrackedPaper(name=item["name"])
        p.title = item.get("title") or p.title
        p.arxiv_id = aid or p.arxiv_id
        p.arxiv_doi = normalize_doi(item.get("arxiv_doi")) or (
            arxiv_doi(p.arxiv_id) if p.arxiv_id else None
        )
        p.journal_doi = normalize_doi(item.get("journal_doi")) or p.journal_doi
        if item.get("authors"):
            p.authors = list(item["authors"])
        state.db.upsert_paper(p)
        console.print(f"[green]✓[/green] {p.name}")


@paper_app.command("list")
def paper_list(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List tracked papers."""
    papers = state.db.list_papers()
    if as_json:
        print(dump_json([p.model_dump() for p in papers]))
        return
    t = Table(header_style="bold")
    for col in ("Name", "Title", "arXiv", "Final DOI", "Citing works", "Last sync"):
        t.add_column(col, overflow="fold")
    for p in papers:
        assert p.id is not None
        n = state.db.conn.execute(
            "SELECT COUNT(*) FROM citations WHERE paper_id=?", (p.id,)
        ).fetchone()[0]
        last = state.db.last_run(p.id)
        t.add_row(
            p.name,
            (p.title or "")[:70],
            p.arxiv_id or "-",
            p.journal_doi or "-",
            str(n),
            f"{last['finished_at'] or last['started_at']} {last['status']}" if last else "-",
        )
    console.print(t)


@paper_app.command("remove")
def paper_remove(name: str) -> None:
    """Stop tracking a paper and delete its citation records."""
    if state.db.delete_paper(name):
        console.print(f"Removed {name}")
    else:
        err.print(f"[red]Unknown paper {name}[/red]")
        raise typer.Exit(2)


# ============================================================================ sync / report


@app.command()
def sync(
    name: Annotated[str | None, typer.Argument(help="Paper name.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Sync every tracked paper.")] = False,
    refresh: Annotated[bool, typer.Option(help="Ignore cached API responses.")] = False,
    offline: Annotated[bool, typer.Option(help="Use cached responses only.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print sync stats as JSON.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
) -> None:
    """Discover citations, deduplicate, classify and reconcile. Safe to run from cron."""
    papers = _papers(name, all_)
    s = state.settings
    s.refresh, s.offline = refresh, offline
    providers = Providers.build(s, state.db)
    out: list[dict[str, Any]] = []
    exit_code = 0

    def progress(msg: str) -> None:
        if not quiet and not as_json:
            err.print(f"[dim]{msg}[/dim]")

    try:
        for p in papers:
            syncer = Syncer(s, state.db, providers, progress=progress)
            try:
                rep = syncer.sync(p)
            except Exception as exc:  # keep going with the other papers
                err.print(f"[red]{p.name}: sync failed: {exc}[/red]")
                exit_code = 1
                continue
            out.append(
                {"paper": rep.paper, "status": rep.status, **rep.stats, "warnings": rep.warnings}
            )
            if not as_json and not quiet:
                acts = ", ".join(f"{k}={v}" for k, v in rep.stats["actions"].items())
                console.print(
                    f"[green]✓[/green] {rep.paper}: {rep.stats['unique_works']} citing "
                    f"works ({rep.stats['raw_records']} provider records) — {acts}"
                )
                for w in rep.warnings:
                    console.print(f"  [yellow]![/yellow] {w}")
    finally:
        providers.close()
    if as_json:
        print(dump_json(out))
    raise typer.Exit(exit_code)


@app.command()
def report(
    name: Annotated[str | None, typer.Argument(help="Paper name.")] = None,
    all_: Annotated[bool, typer.Option("--all")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    details: Annotated[
        bool, typer.Option("--details", "-d", help="Show evidence for every citation.")
    ] = False,
) -> None:
    """Show the reconciliation report."""
    papers = _papers(name, all_)
    if as_json:
        data = [report_json(state.db, p) for p in papers]
        print(dump_json(data if all_ else data[0]))
        return
    for p in papers:
        render_report(console, state.db, p, verbose=details)


class ExportFormat(StrEnum):
    csv = "csv"
    json = "json"


@app.command()
def export(
    fmt: Annotated[ExportFormat, typer.Option("--format", "-f")] = ExportFormat.csv,
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("report.csv"),
    paper: Annotated[str | None, typer.Option("--paper", help="Only this paper.")] = None,
) -> None:
    """Export all (or one paper's) reconciliation rows to CSV or JSON."""
    papers = [_paper_or_exit(paper)] if paper else state.db.list_papers()
    if fmt == ExportFormat.csv:
        rows = export_rows(state.db, papers)
        write_csv(rows, output)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(dump_json([report_json(state.db, p) for p in papers]), encoding="utf-8")
        rows = []
    console.print(f"Wrote {output}" + (f" ({len(rows)} rows)" if rows else ""))


@app.command("export-support")
def export_support_cmd(
    name: Annotated[str, typer.Argument(help="Paper name.")],
    output_dir: Annotated[Path, typer.Option("--output-dir", help="Base directory.")] = Path(
        "output"
    ),
    cited_scopus_link: Annotated[
        str | None,
        typer.Option(
            "--cited-scopus-link",
            help="Scopus record URL of the published article (filled automatically when a "
            "Scopus API key is configured).",
        ),
    ] = None,
) -> None:
    """Write the Scopus-support pack: Excel attachment, request text, CSV and Markdown.

    Nothing is submitted anywhere; review the files and send them yourself if appropriate.
    """
    p = _paper_or_exit(name)
    files, n, missing = export_support(
        state.db, p, output_dir / p.name, cited_link_override=cited_scopus_link
    )
    console.print(f"{n} candidate(s)")
    for f in files:
        console.print(f"  {f}")
    if n == 0:
        console.print(
            "[dim]No preprint-citing candidates with CHECK_SCOPUS / "
            "LIKELY_MISSING_LINK status.[/dim]"
        )
    elif missing:
        console.print(
            f"[yellow]{missing} Scopus link(s) are empty[/yellow] (no Scopus API key, or the "
            "record was not found). Fill them in the .xlsx before attaching it; the last column "
            "has a DOI search link to find each citing article, and --cited-scopus-link sets "
            "the published article's link."
        )


# ============================================================================ manual citations


class CitedVersion(StrEnum):
    preprint = "preprint"
    vor = "vor"
    both = "both"


_CITED_VERSION = {"preprint": "PREPRINT", "vor": "VERSION_OF_RECORD", "both": "BOTH"}


@citation_app.command("add")
def citation_add(
    paper: Annotated[str, typer.Option("--paper", "-p", help="Tracked paper that is cited.")],
    doi: Annotated[str, typer.Option("--doi", help="DOI of the citing article.")],
    cites: Annotated[
        CitedVersion, typer.Option("--cites", help="Which version the reference points to.")
    ] = CitedVersion.preprint,
    note: Annotated[
        str | None, typer.Option("--note", help="Your evidence, e.g. 'checked in Scopus'.")
    ] = None,
    no_sync: Annotated[bool, typer.Option("--no-sync")] = False,
) -> None:
    """Add a citing article by DOI with the cited version you confirmed yourself."""
    target = _paper_or_exit(paper)
    doi_n = normalize_doi(doi)
    if not doi_n:
        err.print(f"[red]Not a valid DOI: {doi}[/red]")
        raise typer.Exit(2)
    assert target.id is not None
    state.db.add_manual_citation(target.id, doi_n, _CITED_VERSION[cites.value], note)
    console.print(f"[green]Added[/green] {doi_n} → {target.name} (cites {cites.value})")
    if not no_sync:
        _sync_one(target)


@citation_app.command("remove")
def citation_remove(
    paper: Annotated[str, typer.Option("--paper", "-p")],
    doi: Annotated[str, typer.Option("--doi")],
) -> None:
    """Remove a manually added citation (takes effect on the next sync)."""
    target = _paper_or_exit(paper)
    assert target.id is not None
    if not state.db.remove_manual_citation(target.id, normalize_doi(doi) or doi):
        err.print("[red]No such manual citation.[/red]")
        raise typer.Exit(2)
    console.print("Removed. Run `citereclaim sync` to update the report.")


@citation_app.command("list")
def citation_list(paper: Annotated[str, typer.Option("--paper", "-p")]) -> None:
    """List manually added citations."""
    target = _paper_or_exit(paper)
    assert target.id is not None
    for doi, row in state.db.manual_citations(target.id).items():
        console.print(f"{doi}  cites {row['target']}  {row['note'] or ''}")


def _sync_one(target: TrackedPaper) -> None:
    s = state.settings
    providers = Providers.build(s, state.db)
    try:
        rep = Syncer(s, state.db, providers, progress=lambda m: err.print(f"[dim]{m}[/dim]")).sync(
            target
        )
    finally:
        providers.close()
    console.print(
        f"[green]✓[/green] {rep.stats['unique_works']} citing works now tracked. "
        f"Run `citereclaim report {target.name}`."
    )


# ============================================================================ scholar


@scholar_app.command("import")
def scholar_import_cmd(
    file: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="CSV, BibTeX (.bib) or JSON export.")
    ],
    paper: Annotated[
        str | None,
        typer.Option(
            "--paper", "-p", help="Tracked paper these citations cite (optional if only one)."
        ),
    ] = None,
    no_sync: Annotated[
        bool, typer.Option("--no-sync", help="Only store; resolve on next sync.")
    ] = False,
) -> None:
    """Import a manually exported Google Scholar 'Cited by' list."""
    if paper:
        target = _paper_or_exit(paper)
    else:
        papers = state.db.list_papers()
        if len(papers) != 1:
            err.print("[red]Several papers are tracked; pass --paper NAME.[/red]")
            raise typer.Exit(2)
        target = papers[0]
    try:
        rows = scholar_import.parse_file(file)
    except (ValueError, UnicodeDecodeError) as exc:
        err.print(f"[red]Could not parse {file}: {exc}[/red]")
        raise typer.Exit(2) from exc
    assert target.id is not None
    added = state.db.add_scholar_rows(
        target.id, str(file), [(scholar_import.row_hash(r), r) for r in rows]
    )
    with_doi = sum(1 for r in rows if r.get("doi"))
    console.print(f"Parsed {len(rows)} rows ({with_doi} with DOI); {added} new for {target.name}.")
    if not no_sync:
        console.print("Resolving and merging via sync ...")
        _sync_one(target)


# ============================================================================ scopus sources


@sources_app.command("update")
def sources_update(
    force: Annotated[
        bool, typer.Option(help="Re-download even if the local copy is fresh.")
    ] = False,
) -> None:
    """Discover and download the current Scopus Source Title List from Elsevier."""
    try:
        with console.status("Discovering and downloading the Scopus Source Title List ..."):
            res = scopus_sources.update(state.db, state.settings, force=force)
    except Exception as exc:
        err.print(
            f"[red]Automatic update failed:[/red] {exc}\n"
            "Download the 'Source title list' .xlsx manually from "
            f"{scopus_sources.CONTENT_PAGE} and run "
            "`citereclaim scopus-sources import FILE`."
        )
        raise typer.Exit(1) from exc
    if res.get("skipped"):
        console.print(f"[dim]Skipped: {res['reason']}[/dim]")
    else:
        console.print(
            f"[green]Imported[/green] {res['sources']} sources, {res['issns']} ISSNs, "
            f"{res['isbns']} proceedings ISBNs ({res.get('version') or '?'}) from "
            f"{res['url']}"
        )


@sources_app.command("import")
def sources_import(file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Import a locally downloaded Scopus Source Title List (.xlsx or .csv)."""
    with console.status(f"Parsing {file} ..."):
        try:
            res = scopus_sources.import_file(state.db, file)
        except ValueError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
    console.print(
        f"[green]Imported[/green] {res['sources']} sources, {res['issns']} ISSNs, "
        f"{res['isbns']} proceedings ISBNs (version {res.get('version') or '?'}; "
        f"sheets: {', '.join(res['sheets'])})"
    )


@sources_app.command("status")
def sources_status(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show which Scopus Source List is loaded and how old it is."""
    st = scopus_sources.status(state.db)
    if as_json:
        print(dump_json(st))
        return
    if not st:
        console.print(
            "[yellow]No Scopus Source List loaded.[/yellow] Run "
            "`citereclaim scopus-sources update`."
        )
        raise typer.Exit(1)
    for k in (
        "version_label",
        "imported_at",
        "age_days",
        "origin",
        "sha256",
        "n_sources",
        "n_issns",
        "n_isbns",
    ):
        console.print(f"{k:>14}: {st[k]}")


@sources_app.command("lookup")
def sources_lookup(
    query: Annotated[str, typer.Argument(help="ISSN or exact journal title.")],
) -> None:
    """Look up an ISSN or journal title in the local Scopus Source List."""
    idx = scopus_sources.SourceIndex(state.db)
    issn = normalize_issn(query)
    rows = idx.by_issn(issn) if issn else idx.by_title(query)
    if not rows:
        console.print("Not in the Scopus Source List.")
        raise typer.Exit(1)
    for r in rows:
        console.print(
            json.dumps(
                {
                    k: r[k]
                    for k in (
                        "title",
                        "issn",
                        "eissn",
                        "coverage",
                        "active",
                        "discontinued",
                        "source_type",
                        "publisher",
                    )
                },
                ensure_ascii=False,
            )
        )


# ============================================================================ cache


@cache_app.command("stats")
def cache_stats() -> None:
    """Show cached responses per provider."""
    for r in state.db.cache_stats():
        console.print(f"{r['provider']:>18}: {r['n']} responses ({r['fresh']} fresh)")


@cache_app.command("clear")
def cache_clear(provider: Annotated[str | None, typer.Option()] = None) -> None:
    """Delete cached responses (all, or one provider)."""
    console.print(f"Deleted {state.db.cache_clear(provider)} cached responses")


def run() -> None:  # pragma: no cover
    sys.exit(app())

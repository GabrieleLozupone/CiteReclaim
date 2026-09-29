from __future__ import annotations

import json
from pathlib import Path

import pytest

from citation_reconciler.config import Settings
from citation_reconciler.db import Database
from citation_reconciler.models import TrackedPaper

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(home=tmp_path / "home")
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings: Settings) -> Database:
    d = Database(settings.db_path)
    yield d
    d.close()


@pytest.fixture
def no_sleep():
    """Collects requested sleeps instead of sleeping."""
    calls: list[float] = []
    return calls, calls.append


@pytest.fixture
def paper() -> TrackedPaper:
    return TrackedPaper(
        id=1,
        name="DEMO",
        title="Latent Diffusion Autoencoders: Toward Efficient and Meaningful Unsupervised "
        "Representation Learning in Medical Imaging",
        authors=["Gabriele Lozupone", "Alessandro Bria"],
        arxiv_id="2504.08635",
        arxiv_doi="10.48550/arxiv.2504.08635",
        journal_doi="10.1016/j.media.2026.103932",
        journal_title="Medical Image Analysis",
        journal_issns=["1361-8415"],
        volume="108",
        year=2026,
    )

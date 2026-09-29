"""Configuration loaded from environment variables and an optional ``.env`` file.

Every credential is optional. The application must work with zero keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

ENV_KEYS = (
    "CROSSREF_MAILTO",
    "SEMANTIC_SCHOLAR_API_KEY",
    "OPENALEX_API_KEY",
    "OPENALEX_MAILTO",
    "ELSEVIER_API_KEY",
    "ELSEVIER_INSTTOKEN",
    "CITERECLAIM_HOME",
)

PROJECT_URL = "https://github.com/GabrieleLozupone/citereclaim"


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal ``.env`` parser (KEY=VALUE, ``#`` comments, optional quotes).

    Values already present in the real environment take precedence.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass
class TTL:
    """Cache lifetimes in seconds."""

    metadata: int = 30 * 24 * 3600
    citations: int = 7 * 24 * 3600
    search: int = 30 * 24 * 3600
    scopus_sources: int = 30 * 24 * 3600
    scopus_api: int = 7 * 24 * 3600
    not_found: int = 24 * 3600


@dataclass
class Settings:
    home: Path
    crossref_mailto: str | None = None
    semantic_scholar_api_key: str | None = None
    openalex_api_key: str | None = None
    openalex_mailto: str | None = None
    elsevier_api_key: str | None = None
    elsevier_insttoken: str | None = None
    ttl: TTL = field(default_factory=TTL)
    offline: bool = False  # serve only from cache, never touch the network
    refresh: bool = False  # bypass cache reads (still writes)
    http_timeout: float = 30.0

    @property
    def db_path(self) -> Path:
        return self.home / "citereclaim.sqlite3"

    @property
    def downloads_dir(self) -> Path:
        return self.home / "downloads"

    @property
    def scopus_api_configured(self) -> bool:
        return bool(self.elsevier_api_key)

    @property
    def contact_email(self) -> str | None:
        return self.crossref_mailto or self.openalex_mailto

    def user_agent(self) -> str:
        ua = f"citereclaim/{__version__} (+{PROJECT_URL}"
        if self.contact_email:
            ua += f"; mailto:{self.contact_email}"
        return ua + ")"

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)


def load_settings(home: Path | None = None, env_file: Path | None = None) -> Settings:
    file_values = load_dotenv(env_file or Path.cwd() / ".env")

    def get(key: str) -> str | None:
        return _clean(os.environ.get(key)) or _clean(file_values.get(key))

    home_value = home or (
        Path(get("CITERECLAIM_HOME")).expanduser() if get("CITERECLAIM_HOME") else None
    )
    return Settings(
        home=home_value or Path.home() / ".citereclaim",
        crossref_mailto=get("CROSSREF_MAILTO"),
        semantic_scholar_api_key=get("SEMANTIC_SCHOLAR_API_KEY"),
        openalex_api_key=get("OPENALEX_API_KEY"),
        openalex_mailto=get("OPENALEX_MAILTO"),
        elsevier_api_key=get("ELSEVIER_API_KEY"),
        elsevier_insttoken=get("ELSEVIER_INSTTOKEN"),
    )

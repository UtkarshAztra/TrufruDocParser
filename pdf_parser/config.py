"""Environment + Databricks client for the PDF parser.

Credentials come from the project-root ``.env``:

    DATABRICKS_HOST=adb-xxxx.azuredatabricks.net
    DATABRICKS_TOKEN=dapi...                    # or leave empty and use a CLI profile
    DATABRICKS_CONFIG_PROFILE=DEFAULT           # used when no PAT, or with --profile
    DATABRICKS_WAREHOUSE_ID=...                 # Pro/Serverless warehouse (AI Functions enabled)
    TRUFRU_PARSER_VOLUME=/Volumes/<catalog>/<schema>/<volume>
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent

DEFAULT_PARSE_VERSION = "2.0"
VOLUME_SUBDIR = "pdf_parser"  # <volume>/pdf_parser/<run_id>/batch_<n>/<file>.pdf


def load_env() -> Optional[Path]:
    """Load the nearest .env walking up from this package (normally the project root)."""
    for parent in [HERE, *HERE.parents]:
        env = parent / ".env"
        if env.is_file():
            load_dotenv(env, override=False)
            return env
    return None


def get_client(profile: Optional[str] = None) -> WorkspaceClient:
    """PAT from .env (DATABRICKS_HOST + DATABRICKS_TOKEN) or a Databricks CLI profile.

    ``ai_parse_document`` can take minutes per batch, so the HTTP/retry timeouts are generous.
    """
    host, token = os.getenv("DATABRICKS_HOST"), os.getenv("DATABRICKS_TOKEN")
    if token and host and not profile:
        if not host.startswith("http"):
            host = "https://" + host
        w = WorkspaceClient(
            config=Config(host=host, token=token, auth_type="pat", http_timeout_seconds=900, retry_timeout_seconds=1200)
        )
        try:
            w.current_user.me()
            return w
        except Exception as exc:  # noqa: BLE001 - any auth failure falls back to the profile
            print(f"  PAT rejected ({str(exc).split('. ')[0]}); falling back to CLI profile")
    for k in ("DATABRICKS_TOKEN", "DATABRICKS_HOST"):
        os.environ.pop(k, None)
    name = profile or os.getenv("DATABRICKS_CONFIG_PROFILE", "DEFAULT")
    try:
        return WorkspaceClient(config=Config(profile=name, http_timeout_seconds=900, retry_timeout_seconds=1200))
    except ValueError as exc:
        first = str(exc).split(". Config:")[0]
        sys.exit(
            f"Databricks auth failed for profile '{name}': {first}\n"
            f"  fix: databricks auth login --profile {name}   (or set a valid DATABRICKS_TOKEN in .env)"
        )


def require_env(name: str, override: Optional[str] = None) -> str:
    value = override or os.getenv(name)
    if not value:
        sys.exit(f"Missing {name} in .env (or pass it on the command line) - see pdf_parser/README.md")
    return value


@dataclass(frozen=True)
class Settings:
    """Everything the Databricks steps need, resolved once in the CLI."""

    volume_root: str
    warehouse_id: str
    parse_version: str = DEFAULT_PARSE_VERSION
    profile: Optional[str] = None
    env_file: Optional[Path] = None

    @classmethod
    def from_env(
        cls,
        volume: Optional[str] = None,
        warehouse: Optional[str] = None,
        parse_version: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> "Settings":
        env_file = load_env()
        return cls(
            volume_root=require_env("TRUFRU_PARSER_VOLUME", volume).rstrip("/"),
            warehouse_id=require_env("DATABRICKS_WAREHOUSE_ID", warehouse),
            parse_version=parse_version or os.getenv("TRUFRU_PARSER_VERSION") or DEFAULT_PARSE_VERSION,
            profile=profile,
            env_file=env_file,
        )

    @property
    def volume_dir(self) -> str:
        return f"{self.volume_root}/{VOLUME_SUBDIR}"

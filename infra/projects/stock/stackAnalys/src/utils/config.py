from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PROJECT_ROOT.parent
ENV_SETTINGS_PATH = "STOCK_SETTINGS_PATH"
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"

# Environment variable to override the database path at runtime
ENV_DB_PATH = "STOCK_DB_PATH"
ENV_DATA_ROOT = "STOCK_DATA_ROOT"
ENV_API_KEYS_PATH = "STOCK_API_KEYS_PATH"


def load_yaml(path: str | Path) -> dict[str, Any]:
    resolved_path = Path(path).expanduser()
    if not resolved_path.is_absolute():
        resolved_path = (PROJECT_ROOT / resolved_path).resolve(strict=False)
    if not resolved_path.exists():
        return {}
    with resolved_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> dict[str, Any]:
    env_settings = os.environ.get(ENV_SETTINGS_PATH)
    if env_settings:
        path = Path(env_settings).expanduser()
        if not path.is_absolute():
            path = REPO_ROOT / path
        return load_yaml(path)
    return load_yaml(SETTINGS_PATH)


def resolve_path(path_value: str | Path) -> Path:
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path.resolve(strict=False)
    return (PROJECT_ROOT / path).resolve(strict=False)


def resolve_data_path(settings: dict[str, Any], key: str, default: str, data_root: str | Path | None = None) -> Path:
    """Resolve a data path, optionally under a configured data root."""
    root_value = data_root or os.environ.get(ENV_DATA_ROOT) or settings.get("data", {}).get("root")
    configured_value = settings.get("data", {}).get(key, default)
    configured_path = Path(configured_value).expanduser()
    if configured_path.is_absolute() or root_value is None:
        return resolve_path(configured_path)
    return resolve_path(root_value) / configured_path


def add_db_path_arg(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add an optional --db-path argument to an existing ArgumentParser.

    If not provided, the path falls back to STOCK_DB_PATH env var,
    then to settings.yaml database.duckdb_path.

    Usage:
        parser = argparse.ArgumentParser()
        add_db_path_arg(parser)
        args = parser.parse_args()
        db_path = resolve_db_path(settings, args.db_path)
    """
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to the DuckDB database file. Overrides STOCK_DB_PATH env var and settings.yaml.",
    )
    return parser


def add_data_root_arg(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add an optional --data-root argument for relocating lake/log/raw data."""
    parser.add_argument(
        "--data-root",
        default=None,
        help="Base directory for relative data paths. Overrides STOCK_DATA_ROOT env var and data.root in settings.yaml.",
    )
    return parser


def resolve_db_path(
    settings: dict[str, Any] | None = None,
    cli_db_path: str | None = None,
) -> Path:
    """Resolve the database path with the following priority:

    1. cli_db_path argument (from --db-path command-line argument)
    2. STOCK_DB_PATH environment variable
    3. settings.yaml database.duckdb_path
    """
    if cli_db_path:
        return resolve_path(cli_db_path)
    env_db = os.environ.get(ENV_DB_PATH)
    if env_db:
        return resolve_path(env_db)
    settings = settings or load_settings()
    return resolve_path(settings["database"]["duckdb_path"])


def resolve_lake_root(settings: dict[str, Any] | None = None, cli_data_root: str | None = None) -> Path:
    settings = settings or load_settings()
    return resolve_data_path(settings, "lake_root", "lake", cli_data_root)


def resolve_log_root(settings: dict[str, Any] | None = None, cli_data_root: str | None = None) -> Path:
    settings = settings or load_settings()
    return resolve_data_path(settings, "log_root", "logs", cli_data_root)


def resolve_report_root(settings: dict[str, Any] | None = None, cli_data_root: str | None = None) -> Path:
    settings = settings or load_settings()
    return resolve_data_path(settings, "report_root", "reports", cli_data_root)


def load_api_keys() -> dict[str, Any]:
    settings = load_settings()
    configured = os.environ.get(ENV_API_KEYS_PATH) or settings.get("api", {}).get("keys_path", "key/api_keys.yaml")
    keys_path = Path(configured).expanduser()
    if not keys_path.is_absolute():
        keys_path = REPO_ROOT / keys_path
    return load_yaml(keys_path)


def ensure_project_dirs(settings: dict[str, Any] | None = None) -> None:
    settings = settings or load_settings()
    for key in ("lake_root", "parquet_root", "raw_root", "log_root", "report_root"):
        if key in settings.get("data", {}):
            resolve_data_path(settings, key, settings["data"][key]).mkdir(parents=True, exist_ok=True)
    resolve_db_path(settings).parent.mkdir(parents=True, exist_ok=True)

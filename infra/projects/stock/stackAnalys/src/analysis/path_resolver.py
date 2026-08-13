from __future__ import annotations

import os
from pathlib import Path


ENV_DATA_ROOT = "STOCK_DATA_ROOT"


def resolve_data_root(db_path: str | Path | None = None) -> Path:
    env_root = os.environ.get(ENV_DATA_ROOT)
    if env_root:
        return Path(env_root).expanduser().resolve(strict=False)

    if db_path:
        path = Path(db_path).expanduser().resolve(strict=False)
        if path.parent.name == "db":
            return path.parent.parent
        return path.parent

    return Path("~/stock_local_ai_data").expanduser().resolve(strict=False)


def resolve_lake_root(db_path: str | Path | None = None) -> Path:
    return resolve_data_root(db_path) / "lake"


def resolve_chart_root(db_path: str | Path | None = None) -> Path:
    return resolve_data_root(db_path) / "artifacts" / "charts"

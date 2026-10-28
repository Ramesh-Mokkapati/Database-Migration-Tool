"""Job files: a migration's connections and options saved as JSON (passwords never stored)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from .connections import ConnectionConfig
from .engine import MigrationOptions


def save_job(path: str | Path, source: ConnectionConfig, target: ConnectionConfig, options: MigrationOptions):
    data = {"source": source.to_dict(), "target": target.to_dict(), "options": asdict(options)}
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_job(path: str | Path) -> tuple[ConnectionConfig, ConnectionConfig, MigrationOptions]:
    """Load a job. Passwords come from the file if present, else DBMIG_SOURCE_PASSWORD / DBMIG_TARGET_PASSWORD."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    source = ConnectionConfig.from_dict(data.get("source", {}))
    target = ConnectionConfig.from_dict(data.get("target", {}))
    source.password = source.password or os.environ.get("DBMIG_SOURCE_PASSWORD", "")
    target.password = target.password or os.environ.get("DBMIG_TARGET_PASSWORD", "")
    return source, target, MigrationOptions.from_dict(data.get("options", {}))

"""Laden der config.toml. Pfade werden relativ zur Datei aufgelöst."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.environ.get("STRESSCELL_CONFIG") or BACKEND_DIR / "config.toml").resolve()
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    resolve_paths(cfg, path.parent)
    cfg["paths"]["config"] = path
    return cfg


def resolve_paths(cfg: dict, root: Path) -> None:
    for key in ("data_dir", "log_dir", "calibration_dir", "sequence_dir"):
        p = Path(os.path.expandvars(os.path.expanduser(str(cfg["paths"][key]))))
        if not p.is_absolute():
            p = (root / p).resolve()
        p.mkdir(parents=True, exist_ok=True)
        cfg["paths"][key] = p
    (cfg["paths"]["log_dir"] / "cell").mkdir(exist_ok=True)

"""Central configuration loader: YAML + environment variables."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::-(.*))?\}")


def _expand_env(value: str) -> str:
    def repl(m: re.Match) -> str:
        key, default = m.group(1), m.group(2)
        return os.environ.get(key, default if default is not None else "")

    return _ENV_PATTERN.sub(repl, value)


def _expand(obj):
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {k: _expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand(v) for v in obj]
    return obj


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        log.warning("Config file %s not found, using defaults + env.", path)
        cfg: dict = {}
    else:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    cfg = _expand(cfg)
    # Env overrides (explicit, documented in README/.env.example)
    neo4j = cfg.setdefault("neo4j", {})
    neo4j.setdefault("uri", os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    neo4j.setdefault("username", os.environ.get("NEO4J_USERNAME", "neo4j"))
    neo4j.setdefault("password", os.environ.get("NEO4J_PASSWORD", "password"))
    neo4j.setdefault("database", os.environ.get("NEO4J_DATABASE", "neo4j"))
    neo4j.setdefault("batch_size", 500)
    neo4j.setdefault("max_retries", 10)
    neo4j.setdefault("retry_delay_seconds", 3)
    cfg.setdefault("spacy_model", os.environ.get("SPACY_MODEL", "en_core_web_lg"))
    cfg.setdefault("batch_size", 32)
    return cfg


def get_neo4j_config(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    return cfg.get("neo4j", {})

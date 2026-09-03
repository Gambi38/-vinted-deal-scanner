"""Cache local du profil de recherches compilé."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


SCHEMA = 2


def search_fingerprint(searches: list, settings: dict) -> str:
    payload = json.dumps(
        {"searches": searches, "settings": settings},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_rule_index(path: Path, fingerprint: str):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if data.get("schema") != SCHEMA or data.get("fingerprint") != fingerprint:
        return None
    rows = data.get("rule_index")
    if not isinstance(rows, list):
        return None
    return [tuple(row) for row in rows if isinstance(row, list) and len(row) == 2]


def save_rule_index(path: Path, fingerprint: str, rule_index: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({
        "schema": SCHEMA,
        "fingerprint": fingerprint,
        "compiled_at": time.time(),
        "rule_index": rule_index,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)

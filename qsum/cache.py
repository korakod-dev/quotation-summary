"""Per-file extraction cache keyed by the SHA-256 of the PDF bytes.

Unchanged PDFs are never re-sent to Claude. Renaming/moving a file keeps its cache hit (hash is
content-based). Entries hold the normalised record, the extraction method and the first-seen date.
The cache never stores the API key or the PDF itself (only extracted fields).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA_VERSION = 1

# A Claude result that fails validation is re-read this many times in total, then kept as is
# (so a genuinely odd quotation does not cost an API call every week). Used by qsum.main and qsum.status.
MAX_CLAUDE_ATTEMPTS = 2


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Cache:
    def __init__(self, path: Path):
        self.path = path
        self.entries: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("schema") == SCHEMA_VERSION:
                self.entries = data.get("entries", {})
        except (OSError, ValueError):
            self.entries = {}   # missing/corrupt cache: start fresh (costs one re-extraction)

    def get(self, sha: str) -> Optional[Dict[str, Any]]:
        return self.entries.get(sha)

    def put(self, sha: str, record: Dict[str, Any], method: str, first_seen: str, model: str = "",
            warned: bool = False, attempts: int = 1) -> None:
        """`warned` = the record failed validation when stored; `attempts` = how many times Claude read it.
        The pipeline re-reads warned Claude results once more (see MAX_CLAUDE_ATTEMPTS)."""
        self.entries[sha] = {"record": record, "method": method, "first_seen": first_seen, "model": model,
                             "warned": warned, "attempts": attempts}

    def save(self) -> None:
        """Atomic write (temp file + rename) so an interrupted run cannot corrupt the cache."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"schema": SCHEMA_VERSION, "entries": self.entries}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

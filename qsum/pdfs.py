"""Finding the PDFs to process.

Kept in its own light module (stdlib only) so that qsum.status can list the folder without importing the
Excel writer / Claude client that qsum.main pulls in. qsum.main re-exports these names.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional


class SourceUnreadable(Exception):
    """The PDF folder cannot be read (macOS privacy block, permissions). Message is shown to the user."""


def unreadable_message(folder: Path, err: OSError) -> str:
    return ("อ่านโฟลเดอร์ %s ไม่ได้ (%s) ถ้าอยู่ใน Documents/Desktop/Downloads macOS จะกันงานตั้งเวลาไว้ "
            "แก้โดยย้ายโฟลเดอร์ PDF ไปไว้นอกโฟลเดอร์เหล่านั้น เช่น ~/QuotationSummary/ แล้วแก้ source_folder ใน config.json"
            % (folder, err.strerror or err))


def list_pdfs(folder: Path, recursive: bool, errors: Optional[List[str]] = None) -> List[Path]:
    """All *.pdf under the folder, skipping hidden files and Office lock files (~$...).

    Raises SourceUnreadable if the top folder cannot be listed. Unreadable sub-folders are appended
    to `errors` (if given) and skipped. (pathlib.rglob silently ignores PermissionError, which would
    produce an empty Excel without any warning; os.walk with onerror does not.)
    """
    try:
        os.listdir(folder)   # explicit probe: raises PermissionError on a TCC-protected folder
    except OSError as e:
        raise SourceUnreadable(unreadable_message(folder, e))

    def on_error(err: OSError) -> None:
        if errors is not None:
            errors.append("อ่านโฟลเดอร์ย่อยไม่ได้ (ข้ามไป): %s (%s)" % (err.filename, err.strerror or err))

    out: List[Path] = []
    for root, dirs, files in os.walk(folder, onerror=on_error):
        dirs[:] = [d for d in dirs if not d.startswith(".")]   # skip hidden sub-folders
        if not recursive:
            dirs[:] = []
        for name in files:
            if name.lower().endswith(".pdf") and not name.startswith((".", "~$")):
                p = Path(root) / name
                if p.is_file():
                    out.append(p)
    return sorted(out, key=lambda p: str(p).lower())

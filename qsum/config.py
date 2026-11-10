"""Configuration loading.

Resolution order for the config file:
  1. --config <path> (CLI)
  2. $QS_CONFIG
  3. <project>/config.json            (created by install.sh from the example)
  4. <project>/config.example.json    (fallback, so a first run still works)

Env overrides (handy for tests / one-off runs): QS_SOURCE_FOLDER, QS_OUTPUT_FILE, QS_MODEL, QS_CLAUDE_BACKEND.

Optional key `state_file` (where the desktop app's run state goes); default = state.json next to the cache file.
The run lock (run.lock) always sits next to the state file.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent
SUMMARY_SHEET = "สรุป"          # name of the final summary sheet
UNMATCHED_SHEET = "ไม่ระบุบริษัท"  # catch-all sheet for sellers not in the config
DEFAULT_CLAUDE_CODE_CONFIG_DIR = "~/Library/Application Support/QuotationSummary/claude-code"
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


class ConfigError(Exception):
    """Raised for an unusable configuration (message is shown to the user)."""


@dataclass
class Company:
    sheet_name: str                 # Excel sheet name (<= 31 chars)
    display_name: str = ""          # full legal name, informational
    tax_id: str = ""                # 13-digit tax id; strongest matching key
    aliases: List[str] = field(default_factory=list)  # name fragments (case/space-insensitive)


@dataclass
class Config:
    source_folder: Path
    output_file: Path
    cache_file: Path
    recursive: bool = True
    extractor: str = "auto"         # auto | claude | text
    claude_model: str = "claude-sonnet-5-5"
    keychain_service: str = "quotation-summary-anthropic"
    max_pdf_mb: float = 30.0
    # Which Claude path reads the PDFs: "claude_code" = headless `claude -p` on the user's subscription
    # (token in Keychain); "api" = Anthropic API key (pay per use).
    claude_backend: str = "claude_code"
    claude_code_path: str = ""      # "" = look for `claude` on PATH / well-known install folders
    claude_code_keychain_service: str = "quotation-summary-claude-code"
    claude_code_timeout_sec: int = 300
    claude_code_max_files_per_run: int = 50    # 0 = unlimited; protects the weekly/5-hour subscription quota
    claude_code_config_dir: str = DEFAULT_CLAUDE_CODE_CONFIG_DIR   # "" = do not set CLAUDE_CONFIG_DIR
    companies: List[Company] = field(default_factory=list)
    config_path: Optional[Path] = None
    state_file: Optional[Path] = None   # run state for the desktop app; None = <cache_file folder>/state.json

    def __post_init__(self) -> None:
        if self.state_file is None:
            self.state_file = self.cache_file.parent / "state.json"

    @property
    def lock_file(self) -> Path:
        """Run lock (flock) next to the state file: one real run at a time."""
        return self.state_file.parent / "run.lock"


def _expand(p: str) -> Path:
    """Expand ~ and env vars in a path string."""
    return Path(os.path.expandvars(os.path.expanduser(p)))


def _clean_sheet_name(name: str) -> str:
    """Excel forbids []:*?/\\ and names over 31 chars."""
    return _BAD_SHEET_CHARS.sub("-", name).strip()[:31]


def resolve_config_path(cli_path: Optional[str] = None) -> Path:
    if cli_path:
        return _expand(cli_path)
    if os.environ.get("QS_CONFIG"):
        return _expand(os.environ["QS_CONFIG"])
    for cand in (PROJECT_DIR / "config.json", PROJECT_DIR / "config.example.json"):
        if cand.exists():
            return cand
    raise ConfigError("ไม่พบไฟล์ config.json (รัน scripts/install.sh หรือคัดลอกจาก config.example.json)")


def load_config(cli_path: Optional[str] = None, folder_override: Optional[str] = None) -> Config:
    path = resolve_config_path(cli_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError("ไม่พบไฟล์ config: %s" % path)
    except json.JSONDecodeError as e:
        raise ConfigError("ไฟล์ config อ่านไม่ได้ (JSON ผิดรูปแบบ) %s บรรทัด %d: %s" % (path, e.lineno, e.msg))

    folder = folder_override or os.environ.get("QS_SOURCE_FOLDER") or data.get("source_folder")
    if not folder:
        raise ConfigError("config ต้องมี source_folder")
    source = _expand(folder)

    out = os.environ.get("QS_OUTPUT_FILE") or data.get("output_file") or ""
    output = _expand(out) if out else source / "สรุปใบเสนอราคา.xlsx"

    extractor = str(data.get("extractor", "auto")).lower()
    if extractor not in ("auto", "claude", "text"):
        raise ConfigError("extractor ต้องเป็น auto | claude | text (ตอนนี้: %r)" % extractor)

    backend = str(os.environ.get("QS_CLAUDE_BACKEND") or data.get("claude_backend", "claude_code")).strip().lower()
    if backend not in ("claude_code", "api"):
        raise ConfigError("claude_backend ต้องเป็น claude_code | api (ตอนนี้: %r)" % backend)
    try:
        timeout_sec = int(data.get("claude_code_timeout_sec", 300))
        max_files = int(data.get("claude_code_max_files_per_run", 50))
    except (TypeError, ValueError):
        raise ConfigError("claude_code_timeout_sec และ claude_code_max_files_per_run ต้องเป็นตัวเลขจำนวนเต็ม")
    if timeout_sec <= 0:
        raise ConfigError("claude_code_timeout_sec ต้องมากกว่า 0 (ตอนนี้: %d)" % timeout_sec)
    if max_files < 0:
        raise ConfigError("claude_code_max_files_per_run ต้องไม่ติดลบ (0 = ไม่จำกัด, ตอนนี้: %d)" % max_files)

    companies: List[Company] = []
    seen = set()
    for c in data.get("companies", []):
        sheet = _clean_sheet_name(str(c.get("sheet_name") or c.get("display_name") or ""))
        if not sheet:
            raise ConfigError("ทุกบริษัทใน config ต้องมี sheet_name")
        if sheet in (SUMMARY_SHEET, UNMATCHED_SHEET):
            raise ConfigError("sheet_name %r ชนกับชีตระบบ" % sheet)
        if sheet.lower() in seen:
            raise ConfigError("sheet_name ซ้ำกัน: %r" % sheet)
        seen.add(sheet.lower())
        companies.append(Company(
            sheet_name=sheet,
            display_name=str(c.get("display_name", "")),
            tax_id=re.sub(r"\D", "", str(c.get("tax_id", ""))),
            aliases=[str(a) for a in c.get("aliases", []) if str(a).strip()],
        ))
    if not companies:
        raise ConfigError("config ต้องมี companies อย่างน้อย 1 บริษัท")

    cache_file = _expand(data.get("cache_file", "~/Library/Application Support/QuotationSummary/cache.json"))
    state_raw = data.get("state_file")
    state_file = _expand(str(state_raw)) if state_raw else cache_file.parent / "state.json"

    return Config(
        source_folder=source,
        output_file=output,
        cache_file=cache_file,
        recursive=bool(data.get("recursive", True)),
        extractor=extractor,
        claude_model=os.environ.get("QS_MODEL") or data.get("claude_model", "claude-sonnet-5-5"),
        keychain_service=str(data.get("keychain_service", "quotation-summary-anthropic")),
        max_pdf_mb=float(data.get("max_pdf_mb", 30)),
        claude_backend=backend,
        claude_code_path=str(data.get("claude_code_path", "") or ""),
        claude_code_keychain_service=str(
            data.get("claude_code_keychain_service", "quotation-summary-claude-code")),
        claude_code_timeout_sec=timeout_sec,
        claude_code_max_files_per_run=max_files,
        claude_code_config_dir=str(data.get("claude_code_config_dir", DEFAULT_CLAUDE_CODE_CONFIG_DIR) or ""),
        companies=companies,
        config_path=path,
        state_file=state_file,
    )

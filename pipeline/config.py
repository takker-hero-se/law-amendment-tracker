"""環境変数から読む型付き設定。すべての上限値・閾値はここに置く。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROVIDERS = ("anthropic", "gemini", "template")
SOURCES = ("egov", "git")


def load_dotenv(path: Path) -> int:
    """KEY=VALUE 形式の .env を読み、まだ設定されていない環境変数だけを設定する。設定した件数を返す。

    依存を増やさないための最小実装。コメント行・空行・`export ` 接頭辞・値の引用符に対応する。ファイルがなければ 0。
    """
    if not path.is_file():
        return 0
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"' + "'")
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in _env(name, default).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    """パイプラインの設定値。`Settings.from_env()` で環境変数から組み立てる。"""

    llm_provider: str = "template"
    anthropic_model: str = "claude-haiku-5-5"
    gemini_model: str = "gemini-2.5-flash"
    source: str = "egov"
    egov_base_url: str = "https://laws.e-gov.go.jp/api/2"
    law_types: list[str] = field(default_factory=lambda: ["Act"])
    category_codes: list[str] = field(default_factory=list)
    watchlist_file: Path = Path("data/watchlist.json")
    git_mirror_dir: Path | None = None
    max_events_per_run: int = 30
    request_interval_sec: float = 0.3
    http_timeout_sec: float = 60.0
    noise_min_changed_chars: int = 40
    noise_min_changed_lines: int = 2
    content_dir: Path = Path("frontend/src/content/amendments")
    state_file: Path = Path("data/state.json")
    llm_input_max_chars: int = 24000
    # GitHub Issue（チケット）。issues_repo が空なら無効。
    issues_repo: str = ""
    issues_token: str = ""
    issues_api_base: str = "https://api.github.com"
    issues_interval_sec: float = 1.0
    site_url: str = ""

    def __post_init__(self) -> None:
        if self.llm_provider not in PROVIDERS:
            raise ValueError(f"LAWTRACK_LLM_PROVIDER must be one of {PROVIDERS}, got {self.llm_provider!r}")
        if self.source not in SOURCES:
            raise ValueError(f"LAWTRACK_SOURCE must be one of {SOURCES}, got {self.source!r}")
        if self.max_events_per_run < 1:
            raise ValueError("LAWTRACK_MAX_EVENTS_PER_RUN must be >= 1")
        if self.request_interval_sec < 0:
            raise ValueError("LAWTRACK_REQUEST_INTERVAL_SEC must be >= 0")
        if self.llm_input_max_chars < 1000:
            raise ValueError("LAWTRACK_LLM_INPUT_MAX_CHARS must be >= 1000")
        if self.issues_repo and "/" not in self.issues_repo:
            raise ValueError("LAWTRACK_ISSUES_REPO must be owner/name")
        if self.issues_repo and not self.issues_token:
            raise ValueError("LAWTRACK_ISSUES_REPO requires LAWTRACK_ISSUES_TOKEN or GITHUB_TOKEN")
        if self.issues_interval_sec < 0:
            raise ValueError("LAWTRACK_ISSUES_INTERVAL_SEC must be >= 0")

    @classmethod
    def from_env(cls) -> Settings:
        mirror = _env("LAWTRACK_GIT_MIRROR_DIR", "")
        return cls(
            llm_provider=_env("LAWTRACK_LLM_PROVIDER", "template"),
            anthropic_model=_env("LAWTRACK_ANTHROPIC_MODEL", "claude-haiku-5-5"),
            gemini_model=_env("LAWTRACK_GEMINI_MODEL", "gemini-2.5-flash"),
            source=_env("LAWTRACK_SOURCE", "egov"),
            egov_base_url=_env("LAWTRACK_EGOV_BASE_URL", "https://laws.e-gov.go.jp/api/2").rstrip("/"),
            law_types=_env_list("LAWTRACK_LAW_TYPES", "Act"),
            category_codes=_env_list("LAWTRACK_CATEGORY_CODES", ""),
            watchlist_file=Path(_env("LAWTRACK_WATCHLIST", "data/watchlist.json")),
            git_mirror_dir=Path(mirror) if mirror else None,
            max_events_per_run=_env_int("LAWTRACK_MAX_EVENTS_PER_RUN", 30),
            request_interval_sec=_env_float("LAWTRACK_REQUEST_INTERVAL_SEC", 0.3),
            http_timeout_sec=_env_float("LAWTRACK_HTTP_TIMEOUT_SEC", 60.0),
            noise_min_changed_chars=_env_int("LAWTRACK_NOISE_MIN_CHANGED_CHARS", 40),
            noise_min_changed_lines=_env_int("LAWTRACK_NOISE_MIN_CHANGED_LINES", 2),
            content_dir=Path(_env("LAWTRACK_CONTENT_DIR", "frontend/src/content/amendments")),
            state_file=Path(_env("LAWTRACK_STATE_FILE", "data/state.json")),
            llm_input_max_chars=_env_int("LAWTRACK_LLM_INPUT_MAX_CHARS", 24000),
            issues_repo=_env("LAWTRACK_ISSUES_REPO", ""),
            issues_token=_env("LAWTRACK_ISSUES_TOKEN", _env("GITHUB_TOKEN", "")),
            issues_api_base=_env("LAWTRACK_ISSUES_API_BASE", "https://api.github.com").rstrip("/"),
            issues_interval_sec=_env_float("LAWTRACK_ISSUES_INTERVAL_SEC", 1.0),
            site_url=_env("SITE_URL", "").rstrip("/"),
        )

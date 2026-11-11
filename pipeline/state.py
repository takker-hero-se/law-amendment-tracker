"""実行状態（前回のスナップショット・処理済み履歴 ID・Git コミット）の永続化。JSON 1 ファイル。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

SEEN_LIMIT = 5000


class State(BaseModel):
    version: int = 1
    last_run_at: str | None = None
    # law_id -> 最新の law_revision_id（e-Gov スナップショット）
    snapshot: dict[str, str] = Field(default_factory=dict)
    # 既に公開した law_revision_id（重複生成の防止）
    seen_revision_ids: list[str] = Field(default_factory=list)
    # Git ミラーの前回コミット
    git_last_commit: str | None = None

    def mark_seen(self, revision_id: str) -> None:
        if revision_id in self.seen_revision_ids:
            return
        self.seen_revision_ids.append(revision_id)
        if len(self.seen_revision_ids) > SEEN_LIMIT:
            del self.seen_revision_ids[: len(self.seen_revision_ids) - SEEN_LIMIT]

    def has_seen(self, revision_id: str) -> bool:
        return revision_id in self.seen_revision_ids

    def touch(self) -> None:
        self.last_run_at = datetime.now(UTC).replace(microsecond=0).isoformat()


def load(path: Path) -> State:
    if not path.exists():
        return State()
    try:
        return State.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"state file {path} is corrupt: {exc}") from exc


def save(state: State, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.model_dump(), ensure_ascii=False, indent=2, sort_keys=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8", newline="\n")
    tmp.replace(path)

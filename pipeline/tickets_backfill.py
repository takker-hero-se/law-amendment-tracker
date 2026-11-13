"""既存の記事 Markdown から GitHub Issue（チケット）を一括起票する。

    python -m pipeline.tickets_backfill            # 未起票の記事だけ起票し、frontmatter に issue_url を書く
    python -m pipeline.tickets_backfill --update   # 起票済みも本文・ラベルを更新する
    python -m pipeline.tickets_backfill --limit 100

状態ファイル（`State.tickets`）で起票済みを判定するので、途中で止めても再実行で続きから処理する。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from pipeline import state as state_mod
from pipeline import tickets
from pipeline.config import Settings, load_dotenv
from pipeline.errors import PipelineError, TicketError
from pipeline.fetcher import split_frontmatter

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m pipeline.tickets_backfill", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="処理する記事の最大数")
    parser.add_argument("--update", action="store_true", help="起票済みの Issue も更新する")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="起動時に読む .env（既存の環境変数は上書きしない）")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def backfill(settings: Settings, *, limit: int | None, update: bool, client: tickets.GitHubIssues | None = None) -> tuple[int, int, int]:
    """(起票・更新した数, 失敗数, 飛ばした数) を返す。"""
    if not settings.issues_repo:
        raise PipelineError("LAWTRACK_ISSUES_REPO is not set")
    state = state_mod.load(Path(settings.state_file))
    own = client is None
    if own:
        client = tickets.GitHubIssues(settings.issues_repo, settings.issues_token, api_base=settings.issues_api_base, timeout=settings.http_timeout_sec, interval_sec=settings.issues_interval_sec)
    assert client is not None
    done = failed = skipped = 0
    try:
        paths = sorted(Path(settings.content_dir).glob("*.md"))
        for path in paths:
            if limit is not None and done + failed >= limit:
                break
            text = path.read_text(encoding="utf-8")
            fm, body = split_frontmatter(text)
            rid = str(fm.get("revision_id") or "")
            if rid in state.tickets and not update:
                skipped += 1
                continue
            url = f"{settings.site_url}/amendments/{path.stem}/" if settings.site_url else None
            try:
                ticket = tickets.build(fm, body, url)
                issue_url = tickets.sync(client, state, ticket)
                tickets.set_issue_url(path, issue_url)
                done += 1
                log.info("ticket %s -> %s", path.name, issue_url)
            except TicketError as exc:
                failed += 1
                log.error("ticket failed for %s: %s", path.name, exc)
    finally:
        if own:
            client.close()
        state.touch()
        state_mod.save(state, Path(settings.state_file))
    return done, failed, skipped


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        load_dotenv(args.env_file)
        settings = Settings.from_env()
        done, failed, skipped = backfill(settings, limit=args.limit, update=args.update)
    except (PipelineError, ValueError) as exc:
        log.error("%s", exc)
        return 1
    print(f"tickets={done} failed={failed} skipped={skipped}")  # noqa: T201
    return 0 if failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

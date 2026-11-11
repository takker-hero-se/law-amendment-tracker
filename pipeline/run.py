"""CLI: 差分検知 → ノイズ除去 → 要約 → Markdown 生成を 1 回実行する。

    python -m pipeline.run --since 2026-10-01
    python -m pipeline.run --dry-run            # 検知と除外理由だけ表示
    python -m pipeline.run --law-id 415AC0000000057 --since 2024-01-01   # 1 法令だけ
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from pipeline import filter as noise_filter
from pipeline import generator, summarizer
from pipeline import state as state_mod
from pipeline.config import Settings
from pipeline.egov import EgovClient
from pipeline.errors import PipelineError
from pipeline.fetcher import EgovSource, GitSource, WatchEntry, load_watchlist
from pipeline.models import Amendment

log = logging.getLogger("pipeline")


@dataclass
class RunReport:
    detected: int = 0
    kept: int = 0
    dropped: int = 0
    written: int = 0
    failed: int = 0
    dropped_reasons: list[str] | None = None

    def as_text(self) -> str:
        return f"detected={self.detected} kept={self.kept} dropped={self.dropped} written={self.written} failed={self.failed}"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pipeline.run", description="法改正トラッカーのパイプライン")
    parser.add_argument("--since", type=date.fromisoformat, default=None, help="この公布日以降の改正だけを対象にする (YYYY-MM-DD)")
    parser.add_argument("--law-id", action="append", default=[], help="監視リストの代わりにこの法令 ID だけを見る（複数可）")
    parser.add_argument("--limit", type=int, default=None, help="1 回に処理する最大件数（既定は設定値）")
    parser.add_argument("--dry-run", action="store_true", help="Markdown を書かず、検知結果と除外理由だけ表示")
    parser.add_argument("--no-state", action="store_true", help="状態ファイルを読み書きしない")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def build_source(settings: Settings, law_ids: list[str], client: EgovClient | None) -> EgovSource | GitSource:
    if settings.source == "git":
        if settings.git_mirror_dir is None:
            raise PipelineError("LAWTRACK_GIT_MIRROR_DIR is required for the git source")
        return GitSource(settings.git_mirror_dir)
    if client is None:
        raise PipelineError("egov source requires a client")
    if law_ids:
        watchlist = [WatchEntry({"law_id": law_id}) for law_id in law_ids]
        return EgovSource(client, watchlist)
    watchlist = load_watchlist(settings.watchlist_file)
    scan_types = settings.law_types if settings.category_codes else []
    return EgovSource(client, watchlist, scan_law_types=scan_types, scan_category_codes=settings.category_codes)


def execute(settings: Settings, args: argparse.Namespace, *, client: EgovClient | None = None, provider: summarizer.Provider | summarizer.TemplateProvider | None = None) -> RunReport:
    report = RunReport(dropped_reasons=[])
    state_path = Path(settings.state_file)
    state = state_mod.State() if args.no_state else state_mod.load(state_path)
    own_client = client is None and settings.source == "egov"
    if own_client:
        client = EgovClient(settings.egov_base_url, timeout=settings.http_timeout_sec, interval_sec=settings.request_interval_sec)
    try:
        source = build_source(settings, list(args.law_id), client)
        limit = args.limit or settings.max_events_per_run
        events = source.detect(state, since=args.since, limit=limit)
        report.detected = len(events)
        kept, dropped = noise_filter.apply(events, min_changed_chars=settings.noise_min_changed_chars, min_changed_lines=settings.noise_min_changed_lines)
        report.kept, report.dropped = len(kept), len(dropped)
        for event, reason in dropped:
            report.dropped_reasons.append(f"{event.event_id}: {reason}")  # type: ignore[union-attr]
            log.info("drop %s (%s): %s", event.event_id, event.law.law_title, reason)
            state.mark_seen(event.revision_id)
        if args.dry_run:
            for event in kept:
                log.info("keep %s (%s) +%d/-%d", event.event_id, event.law.law_title, event.stats.added_lines, event.stats.removed_lines)
            return report
        provider = provider or summarizer.build_provider(settings)
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        for event in kept:
            hint = source.category_hint(event.law.law_id) if isinstance(source, EgovSource) else None
            try:
                summary = summarizer.summarize(event, provider, max_input_chars=settings.llm_input_max_chars, category_hint=hint)
            except PipelineError as exc:
                report.failed += 1
                log.error("summarize failed for %s: %s", event.event_id, exc)
                continue
            record = Amendment(event=event, summary=summary, generated_at=now, provider=provider.name)
            path = generator.write(record, Path(settings.content_dir))
            state.mark_seen(event.revision_id)
            report.written += 1
            log.info("wrote %s", path)
        return report
    finally:
        if own_client and client is not None:
            client.close()
        if not args.no_state and not args.dry_run:
            state.touch()
            state_mod.save(state, state_path)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        report = execute(settings, args)
    except (PipelineError, ValueError) as exc:
        log.error("%s", exc)
        return 1
    print(report.as_text())
    if args.dry_run and report.dropped_reasons:
        for line in report.dropped_reasons:
            print("dropped:", line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

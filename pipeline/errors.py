"""パイプライン共通の例外。外部ソースの失敗は必ずこの型に変換する（握りつぶさない）。"""


class PipelineError(Exception):
    """パイプライン内で回復不能な失敗。"""


class SourceError(PipelineError):
    """差分取得元（e-Gov API / Git ミラー）の失敗。"""


class SummarizerError(PipelineError):
    """LLM 要約の失敗（スキーマ不一致・API エラー）。"""


class TicketError(PipelineError):
    """GitHub Issue（チケット）の起票・更新の失敗。"""


class SchemaMismatch(SummarizerError):
    """LLM の出力が Summary スキーマを通らなかった（SDK 側の検証を含む）。summarize() が 1 回だけ再試行する。"""

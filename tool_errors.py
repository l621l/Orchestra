"""Errors whose messages are safe to return to a model."""


class ToolError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def model_result(name, result):
    """Keep search diagnostics for tracing, not in every model response."""
    if name not in {"search_text", "search_regex"}:
        return result
    fields = ("matches", "matches_returned", "truncated", "next_cursor",
              "collection_truncated", "files_skipped", "snapshot_id")
    return {key: result[key] for key in fields if key in result}

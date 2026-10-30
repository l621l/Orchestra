"""Errors whose messages are safe to return to a model."""


class ToolError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def model_result(name, result):
    """Keep search diagnostics for tracing, not in every model response."""
    if name == 'run_check':
        import re
        compact = dict(result)
        output = result.get('output','')
        kept = [line for line in output.splitlines()
                if not re.match(r'^test\S*.* \.\.\. ok$',line)]
        compact['output'] = '\n'.join(kept)
        if result.get('passed') and len(compact['output'])>1800:
            compact['output'] = '[Earlier successful output archived in log]\n' + compact['output'][-1800:]
        compact['output_compacted'] = compact['output'] != output
        return compact
    if name in {'create_file', 'replace_text', 'apply_patch'}:
        # The assistant already generated the edit. Avoid echoing entire files
        # and workspace status into every subsequent model request.
        compact = {key: value for key, value in result.items()
                   if key not in {'diff', 'status', 'status_truncated'}}
        compact.pop('diff_truncated', None)
        memory = compact.get('interface_memory')
        if memory:
            compact['interface_memory'] = {key:memory[key] for key in
                ('updated','notes_saved','notes_stale') if key in memory}
            if memory.get('warnings'):
                compact['interface_memory']['warnings']=memory['warnings']
        return compact
    if name not in {"search_text", "search_regex"}:
        return result
    fields = ("matches", "matches_returned", "truncated", "next_cursor",
              "collection_truncated", "files_skipped", "snapshot_id")
    return {key: result[key] for key in fields if key in result}

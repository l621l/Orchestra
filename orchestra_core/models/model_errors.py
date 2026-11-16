"""Model protocol errors and safe user-facing descriptions."""
import traceback
from pathlib import Path

from orchestra_core.workspace.tool_errors import ToolError


class ModelResponseError(RuntimeError):
    """Known protocol failure with a safe, user-facing explanation."""


class TaskCostLimit(ModelResponseError):
    """The next request does not fit the available task budget."""


class OutputLimitError(ModelResponseError):
    def __init__(self, message, draft=None, tool_drafts=None):
        super().__init__(message)
        self.draft = draft
        self.tool_drafts = tool_drafts or []


def error_description(error):
    # Never dump provider response bodies: they may contain request credentials.
    if isinstance(error, (ModelResponseError, ToolError)):
        return str(error)
    name = type(error).__name__
    if isinstance(error, TimeoutError) or name == 'APITimeoutError':
        return 'Model request timed out before a complete response arrived.'
    if name == 'APIConnectionError':
        return 'Could not connect to the model provider. Check network and provider URL.'
    status = getattr(error, 'status_code', None)
    if type(status) is int:
        reasons = {400: 'Provider rejected the request parameters.',
                   401: 'Provider rejected authentication; check the API key.',
                   403: 'Provider denied access to this model.',
                   404: 'Provider endpoint or model was not found.',
                   429: 'Provider rate or quota limit reached.'}
        return f'HTTP {status}: ' + reasons.get(status, 'Provider returned an API error.')
    frames = traceback.extract_tb(error.__traceback__)
    location = (f' at {Path(frames[-1].filename).name}:{frames[-1].lineno} in {frames[-1].name}'
                if frames else '')
    return f'{name}{location}: unexpected local failure.'

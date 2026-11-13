"""Explicit console commands; ordinary text follows the unfinished task."""


def input_command(text):
    stripped=text.strip()
    lowered=stripped.casefold()
    if lowered in {'c','с','продолжи','продолжить','continue','resume'}:return 'continue',''
    if lowered in {'s','ы','status'}:return 'status',''
    if lowered in {'q','й','exit','quit'}:return 'exit',''
    if lowered in {'n','т'}:return 'new',''
    for prefix in ('n:','т:','новая задача:','новое задание:'):
        if lowered.startswith(prefix):return 'new',stripped[len(prefix):].strip()
    for prefix in ('d:', 'в:', 'debug:', 'дебаг:'):
        if lowered.startswith(prefix):
            return 'debug', stripped[len(prefix):].strip()
    return 'text',stripped


def continuation_intent(text,has_checkpoint):
    command,payload=input_command(text)
    if command in {'new', 'debug'}:return False,''
    if command=='continue':return True,''
    return bool(has_checkpoint),payload if has_checkpoint else ''

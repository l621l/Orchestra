"""Shared model configuration and context formatting."""


def clip(text, limit):
    text = str(text or '')
    if len(text) <= limit:
        return text
    marker = '\n[omitted; read source for full details]\n'
    room = max(0, limit-len(marker))
    return text[:room*2//3]+marker+text[-(room-room*2//3):] if room else marker[:limit]


def role_settings(config, role):
    if role not in {'worker', 'writer'}:
        raise ValueError('Direct supports one writer.')
    writer = config.get('roles', {}).get('writer', {})
    model = writer.get('model', '') if isinstance(writer, dict) else ''
    family = model.lower().partition('-')[0] if isinstance(model, str) else ''
    transport = {'mimo': 'chat', 'gpt': 'responses'}.get(family)
    if (not transport or writer.get('provider') not in config.get('providers', {})
            or writer.get('transport', transport) != transport):
        raise ValueError('Configure roles.writer with MiMo (chat) or GPT (responses).')
    return {**writer, 'transport': transport}

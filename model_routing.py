"""Shared model configuration and context formatting."""


def clip(text, limit):
    text = str(text or '')
    if len(text) <= limit:
        return text
    marker = '\n[omitted; read source for full details]\n'
    room = max(0, limit-len(marker))
    return text[:room*2//3]+marker+text[-(room-room*2//3):] if room else marker[:limit]


def role_settings(config, role):
    role = 'writer' if role == 'worker' else role
    if role not in {'writer', 'strong'}:
        raise ValueError('Unsupported alternating role: '+role)
    roles = config.get('roles', {})
    providers = config.get('providers', {})
    config = roles.get(role)
    if role == 'strong' and config is None:
        config = roles.get('director')
    if (not isinstance(config, dict)
            or config.get('provider') not in providers
            or not isinstance(config.get('model'), str) or not config['model'].strip()
            or config.get('transport', 'responses' if role == 'strong' else 'chat') not in {'chat', 'responses'}):
        raise ValueError('Configure roles.'+role+' with provider, model and transport (chat/responses).')
    return config

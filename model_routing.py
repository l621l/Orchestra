"""Shared model configuration and context formatting."""


def reasoning_levels(provider, model):
    """Conservative capability list; unknown models use their API default."""
    model = model.lower()
    if provider == 'gpt' and model in {
        'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna',
        'gpt-6.1-sol', 'gpt-6.1-astra', 'gpt-6.1-terra',
    }:
        return ('low', 'medium', 'high', 'xhigh', 'max')
    if provider == 'gpt':
        if model in {'gpt-5', 'gpt-5-mini', 'gpt-5-nano'}:
            return ('minimal', 'low', 'medium', 'high')
        if model == 'gpt-5.1':
            return ('none', 'low', 'medium', 'high')
        if model == 'gpt-5.2':
            return ('none', 'low', 'medium', 'high', 'xhigh')
        if model == 'gpt-5.2-pro':
            return ('medium', 'high', 'xhigh')
        if model in {'o1', 'o3', 'o3-mini', 'o4-mini'}:
            return ('low', 'medium', 'high')
    return ()


def clip(text, limit):
    text = str(text or '')
    if len(text) <= limit:
        return text
    marker = '\n[omitted; read source for full details]\n'
    room = max(0, limit-len(marker))
    return text[:room*2//3]+marker+text[-(room-room*2//3):] if room else marker[:limit]


def role_settings(config, role):
    if role not in {'worker', 'writer', 'reviewer'}:
        raise ValueError('Direct supports one writer.')
    roles = config.get('roles', {})
    key = 'reviewer' if role == 'reviewer' and 'reviewer' in roles else 'writer'
    writer = roles.get(key, {})
    model = writer.get('model', '') if isinstance(writer, dict) else ''
    family = model.lower().partition('-')[0] if isinstance(model, str) else ''
    provider_name = writer.get('provider')
    provider = config.get('providers', {}).get(provider_name)
    fixed = {'mimo': 'chat', 'gpt': 'responses'}
    transport = fixed.get(provider_name) or (provider or {}).get('transport') or fixed.get(family)
    if (not isinstance(model, str) or not model.strip() or provider is None
            or transport not in {'chat', 'responses'}
            or (provider_name in fixed and family in fixed and fixed[family] != transport)
            or writer.get('transport', transport) != transport):
        raise ValueError(f'Configure roles.{key} with a supported model and matching API transport.')
    effort = writer.get('reasoning_effort', 'default')
    if effort != 'default' and effort not in reasoning_levels(provider_name, model):
        raise ValueError(f'Unsupported reasoning effort for {provider_name} / {model}: {effort}')
    return {'thinking_parameter': provider_name == 'mimo', **writer, 'transport': transport}

"""Import public list prices for the app's supported direct providers."""
import math
from datetime import datetime, timezone
from urllib.parse import urlsplit

CATALOG_URL = 'https://models.dev/api.json'
DIRECT_PROVIDERS = {
    'gpt': ('openai', 'api.openai.com'),
    'claude': ('anthropic', 'api.anthropic.com'),
    'mimo': ('xiaomi', 'api.xiaomimimo.com'),
    'deepseek': ('deepseek', 'api.deepseek.com'),
    'qwen': ('alibaba', 'dashscope-intl.aliyuncs.com'),
    'openrouter': ('openrouter', 'openrouter.ai'),
}
FIELDS = {
    'input': 'input_cache_miss', 'output': 'output',
    'cache_read': 'input_cache_hit', 'cache_write': 'input_cache_write',
}
DEFAULT_SELECTED = ('mimo', 'gpt', 'deepseek', 'claude')


def discover_providers(catalog):
    return {
        name: {'name': block.get('name') or name, 'api': block.get('api') or '',
               'models': {model: {key: spec[key] for key in
                                   ('status', 'tool_call', 'cost', 'release_date', 'last_updated') if key in spec}
                          for model, spec in block['models'].items() if isinstance(spec, dict)}}
        for name, block in catalog.items()
        if isinstance(block, dict) and isinstance(block.get('models'), dict) and block['models']
    }


def default_selection(pricing):
    discovered = pricing.get('catalog_providers', {})
    selection = {}
    for provider in DEFAULT_SELECTED:
        ready = pricing['providers'].get(provider, {})
        catalog_id, _ = DIRECT_PROVIDERS[provider]
        specs = discovered.get(catalog_id, {}).get('models', {})
        eligible = [name for name in ready
                    if specs.get(name, {}).get('status') != 'deprecated'
                    and specs.get(name, {}).get('tool_call', True)]
        newest = sorted(eligible, key=lambda name: (
            specs.get(name, {}).get('release_date') or specs.get(name, {}).get('last_updated') or ''
        ), reverse=True)
        selection[provider] = {'enabled': True, 'models': newest[:3]}
    return selection


def catalog_rates(spec):
    raw = spec.get('cost') or {}
    if not isinstance(raw, dict):
        return {}
    return {target_key: raw[key] for key, target_key in FIELDS.items()
            if type(raw.get(key)) in (int, float) and math.isfinite(raw[key]) and raw[key] >= 0}


def update_prices(pricing, catalog, providers, bundled):
    if not isinstance(catalog, dict) or not any(key in catalog for key, _ in DIRECT_PROVIDERS.values()):
        raise ValueError('Unrecognized model catalog.')
    changed = matched = 0
    timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    synced = pricing.setdefault('catalog_updated', {})
    manual = pricing.setdefault('manual_rates', {})
    for provider, connection in providers.items():
        address = connection.get('base_url') or ''
        catalog_id, host = DIRECT_PROVIDERS.get(provider, (provider, None))
        block = catalog.get(catalog_id)
        if host is None:
            endpoint = block.get('api') if isinstance(block, dict) else None
            if not endpoint or address.rstrip('/') != endpoint.rstrip('/'):
                continue
        elif (address and urlsplit(address).hostname != host) or (not address and provider != 'gpt'):
            continue
        models = block.get('models', {}) if isinstance(block, dict) else {}
        if not isinstance(models, dict):
            continue
        target = pricing['providers'].setdefault(provider, {})
        stamps = synced.setdefault(provider, {})
        protected = manual.setdefault(provider, [])
        for model, spec in models.items():
            if (not isinstance(spec, dict) or spec.get('status') == 'deprecated'
                    or not spec.get('tool_call', True)):
                continue
            rates = catalog_rates(spec)
            if not {'input_cache_miss', 'output'} <= rates.keys():
                continue
            old = target.get(model)
            if old is not None and model not in stamps and old != bundled.get(provider, {}).get(model):
                if model not in protected:
                    protected.append(model)
            if model in protected:
                continue
            combined = {**(old or {}), **rates}
            combined.setdefault('input_cache_hit', combined['input_cache_miss'])
            target[model] = combined
            stamps[model] = timestamp
            matched += 1
            changed += old != combined
    return matched, changed

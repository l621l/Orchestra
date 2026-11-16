import unittest

from orchestra_core.models.model_catalog import default_selection, discover_providers, update_prices


class ModelCatalogTests(unittest.TestCase):
    def test_defaults_pick_three_newest_priced_models_and_discover_other_providers(self):
        source = {'openai': {'models': {f'gpt-{i}': {'release_date': f'2026-01-0{i}',
                                                    'cost': {'input': 1, 'output': 2}}
                                        for i in range(1, 6)}},
                  'other': {'name': 'Another provider', 'models': {'example': {}}}}
        pricing = {'providers': {}}
        update_prices(pricing, source, {'gpt': {'base_url': 'https://api.openai.com/v1'}}, {})
        pricing['catalog_providers'] = discover_providers(source)
        selection = default_selection(pricing)
        self.assertEqual(set(selection), {'mimo', 'gpt', 'deepseek', 'claude'})
        self.assertEqual(selection['gpt']['models'], ['gpt-5', 'gpt-4', 'gpt-3'])
        self.assertIn('other', pricing['catalog_providers'])

    def test_updates_matching_direct_models_and_preserves_local_prices(self):
        bundled = {'gpt': {'managed': {'input_cache_miss': 1, 'input_cache_hit': 0.1, 'output': 4}}}
        manual = {'input_cache_miss': 9, 'input_cache_hit': 8, 'output': 7}
        pricing = {'providers': {'gpt': {**bundled['gpt'], 'local': manual}, 'mimo': {}}}
        catalog = {'openai': {'models': {
            'managed': {'cost': {'input': 2, 'output': 8, 'cache_write': 3}},
            'new': {'cost': {'input': 5, 'output': 9}, 'tool_call': True},
            'local': {'cost': {'input': 1, 'output': 1}},
            'no-tools': {'cost': {'input': 1, 'output': 1}, 'tool_call': False},
        }}, 'xiaomi': {'models': {'proxy': {'cost': {'input': 1, 'output': 1}}}}}
        connections = {'gpt': {'base_url': 'https://api.openai.com/v1'},
                       'mimo': {'base_url': 'https://example.com/v1'}}
        self.assertEqual(update_prices(pricing, catalog, connections, bundled), (2, 2))
        models = pricing['providers']['gpt']
        self.assertEqual(models['managed'], {'input_cache_miss': 2, 'input_cache_hit': 0.1,
                                             'input_cache_write': 3, 'output': 8})
        self.assertEqual(models['new']['input_cache_hit'], 5)
        self.assertEqual(models['local'], manual)
        self.assertNotIn('no-tools', models)
        self.assertEqual(pricing['providers']['mimo'], {})
        self.assertEqual(update_prices(pricing, catalog, connections, bundled), (2, 0))

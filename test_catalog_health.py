from collections import defaultdict
import tempfile
from pathlib import Path
import json
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
import vinted_api_light as bot
from web_catalog import catalog_payload


class CatalogHealthTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, payload):
        items = catalog_payload(payload, trusted_search_response=True)
        reader = SimpleNamespace(read=AsyncMock())
        if items is None:
            reader.read.side_effect = bot.CatalogUnavailable('Schéma inconnu')
        else:
            reader.read.return_value = items
        stats = defaultdict(int)
        await bot.check_catalog_health('https://www.vinted.be', AsyncMock(),
            SimpleNamespace(catalog_reader=reader), {}, stats, None)
        return stats

    async def test_404_stops_after_one_navigation(self):
        reader = SimpleNamespace(read=AsyncMock(side_effect=bot.CatalogUnavailable('HTTP 404')))
        stats = defaultdict(int)
        with self.assertRaisesRegex(bot.CatalogUnavailable, 'HTTP 404'):
            await bot.check_catalog_health('https://www.vinted.be', AsyncMock(),
                SimpleNamespace(catalog_reader=reader), {}, stats, None)
        self.assertEqual(reader.read.await_count, 1)
        self.assertEqual(stats['catalog_success'], 0)

    async def test_valid_empty_catalog_is_healthy(self):
        stats = await self.probe({'items': []})
        self.assertEqual(stats['catalog_success'], 1)

    async def test_error_json_is_not_a_catalog(self):
        for data in ({'error': 'unavailable'}, [], {'items': None}, {'items': [1]}, {'items': [{'id': 1}]}):
            with self.subTest(data=data), self.assertRaises(bot.CatalogUnavailable):
                await self.probe(data)

    async def test_valid_items_are_accepted(self):
        stats = await self.probe({'items': [{'id': 42, 'title': 'Nintendo Switch', 'price': {'amount': '40', 'currency_code': 'EUR'}}]})
        self.assertEqual(stats['catalog_items'], 1)

    async def test_failure_is_persisted_and_propagated(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(bot, 'DATA_DIR', Path(folder)), patch.object(
                bot, 'main_async', AsyncMock(side_effect=bot.CatalogUnavailable('HTTP 404'))
            ):
                with self.assertRaises(bot.CatalogUnavailable):
                    await bot.run_workflow_session()
            report = json.loads((Path(folder) / 'dernier_echec.json').read_text())
            self.assertEqual(report['error_type'], 'CatalogUnavailable')
            self.assertEqual(report['cycle'], 1)

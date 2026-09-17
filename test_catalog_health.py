import asyncio
from collections import defaultdict
import tempfile
from pathlib import Path
import json
import unittest
from unittest.mock import AsyncMock, patch

import vinted_api_light as bot


class Response:
    def __init__(self, status, data):
        self.status, self.data, self.headers = status, data, {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.data


class Session:
    def __init__(self, response):
        self.response, self.calls = response, 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self.response


class CatalogHealthTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, status, payload):
        stats = defaultdict(int)
        session = Session(Response(status, payload))
        await bot.check_catalog_health('https://www.vinted.be', AsyncMock(),
                                       session, {}, stats, None)
        return session, stats

    async def test_404_stops_after_one_request(self):
        session = Session(Response(404, {}))
        with self.assertRaisesRegex(bot.CatalogUnavailable, 'HTTP 404'):
            await bot.check_catalog_health('https://www.vinted.be', AsyncMock(),
                                           session, {}, defaultdict(int), None)
        self.assertEqual(session.calls, 1)

    async def test_valid_empty_catalog_is_healthy(self):
        _, stats = await self.probe(200, {'items': []})
        self.assertEqual(stats['catalog_success'], 1)

    async def test_error_json_is_not_a_catalog(self):
        for data in ({'error': 'unavailable'}, [], {'items': None}, {'items': [1]}):
            with self.subTest(data=data), self.assertRaises(bot.CatalogUnavailable):
                await self.probe(200, data)

    async def test_valid_items_are_accepted(self):
        _, stats = await self.probe(200, {'items': [{'id': 42}]})
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


if __name__ == '__main__':
    unittest.main()

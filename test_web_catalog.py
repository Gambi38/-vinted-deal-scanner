"""Fixtures synthétiques : ne prouvent pas le schéma actuel du site Vinted."""
import json
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit, parse_qs
from web_catalog import WebCatalog, CatalogUnavailable, catalog_payload, embedded_catalog, search_url, matches_search

ITEM = {'id': 42, 'title': 'Nintendo Switch', 'price': {'amount': '40', 'currency_code': 'EUR'}}

def html(items):
    return '<script type="application/json">' + json.dumps({'props': {'catalog': {'items': items}}}) + '</script>'

class ParserTests(unittest.TestCase):
    def test_public_url_encodes_search(self):
        url = search_url('https://www.vinted.be', 'switch & jeux', 40, 2)
        self.assertEqual(urlsplit(url).path, '/catalog')
        self.assertEqual(parse_qs(urlsplit(url).query)['search_text'], ['switch & jeux'])
        self.assertNotIn('/api/', url)

    def test_unrelated_items_are_not_a_catalog(self):
        self.assertIsNone(catalog_payload({'recommendations': {'items': [ITEM]}}))
        self.assertIsNone(catalog_payload({'items': [ITEM]}))

    def test_embedded_catalog_preserves_unknown_rating_age(self):
        item = embedded_catalog(html([ITEM]))[0]
        self.assertNotIn('user', item)
        self.assertNotIn('created_at', item)
        self.assertEqual(item, ITEM)

    def test_empty_and_unsupported_are_different(self):
        self.assertEqual(embedded_catalog(html([])), [])
        self.assertIsNone(embedded_catalog('<html>Vinted</html>'))
        self.assertIsNone(embedded_catalog('<script type="application/json">invalid</script>'))

    def test_response_must_match_origin_query_and_page(self):
        self.assertTrue(matches_search('https://www.vinted.be/anything?search_text=nintendo&page=2', 'nintendo', 2))
        self.assertFalse(matches_search('https://evil.test/anything?search_text=nintendo&page=2', 'nintendo', 2))
        self.assertFalse(matches_search('https://www.vinted.be/x?search_text=nintendo&page=1', 'nintendo', 2))
        self.assertFalse(matches_search('https://www.vinted.be/x?search_text=ps5&page=2', 'nintendo', 2))

class ReaderTests(unittest.IsolatedAsyncioTestCase):
    def reader(self, status=200, document=None, body='Vinted'):
        page = SimpleNamespace(
            on=Mock(), remove_listener=Mock(),
            goto=AsyncMock(return_value=SimpleNamespace(status=status, headers={})),
            wait_for_load_state=AsyncMock(), content=AsyncMock(return_value=document or '<html></html>'),
            locator=Mock(return_value=SimpleNamespace(inner_text=AsyncMock(return_value=body))),
            close=AsyncMock(),
        )
        reader = WebCatalog('https://www.vinted.be', {})
        reader.context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        return reader, page

    async def test_embedded_read_and_page_cleanup(self):
        reader, page = self.reader(document=html([ITEM]))
        result = await reader.read('nintendo', None, 1, 50, AsyncMock(), defaultdict(int))
        self.assertEqual(result, [ITEM])
        page.close.assert_awaited_once()
        self.assertIn('/catalog?', page.goto.call_args.args[0])

    async def test_unrecognized_200_is_failure(self):
        reader, page = self.reader()
        with self.assertRaisesRegex(CatalogUnavailable, 'aucune liste structurée'):
            await reader.read('nintendo', None, 1, 50, AsyncMock(), defaultdict(int))
        page.close.assert_awaited_once()

    async def test_access_refusal_stops_queued_navigations(self):
        for status in (401, 403, 429):
            reader, page = self.reader(status=status)
            limiter = AsyncMock()
            for _ in range(2):
                with self.assertRaises(CatalogUnavailable):
                    await reader.read('nintendo', None, 1, 50, limiter, defaultdict(int))
            self.assertEqual(page.goto.await_count, 1)
            limiter.register_response.assert_awaited_once_with(status, {}, 'catalog')
            page.close.assert_awaited_once()

    async def test_challenge_is_not_empty_result(self):
        reader, page = self.reader(body='Verify you are human')
        with self.assertRaisesRegex(CatalogUnavailable, 'Vérification humaine'):
            await reader.read('nintendo', None, 1, 50, AsyncMock(), defaultdict(int))
        self.assertIsNotNone(reader.stopped)

    async def test_404_is_explicit(self):
        reader, page = self.reader(status=404)
        with self.assertRaisesRegex(CatalogUnavailable, 'HTTP 404'):
            await reader.read('nintendo', None, 1, 50, AsyncMock(), defaultdict(int))
        page.close.assert_awaited_once()

    async def test_network_failure_closes_page(self):
        reader, page = self.reader()
        page.goto.side_effect = TimeoutError()
        with self.assertRaisesRegex(CatalogUnavailable, 'TimeoutError'):
            await reader.read('nintendo', None, 1, 50, AsyncMock(), defaultdict(int))
        page.close.assert_awaited_once()

    async def test_observed_matching_json_response_is_used(self):
        reader, page = self.reader()
        callbacks = {}
        page.on.side_effect = lambda event, cb: callbacks.update({event: cb})
        response = SimpleNamespace(
            url='https://www.vinted.be/site-chosen-route?search_text=nintendo&page=1',
            request=SimpleNamespace(resource_type='fetch'), status=200,
            headers={'content-type': 'application/json'},
            json=AsyncMock(return_value={'items': [ITEM]}),
        )
        async def navigate(*args, **kwargs):
            callbacks['response'](response)
            return SimpleNamespace(status=200, headers={})
        page.goto.side_effect = navigate
        self.assertEqual(await reader.read('nintendo', None, 1, 50, AsyncMock(), defaultdict(int)), [ITEM])
        page.close.assert_awaited_once()

    async def test_missing_reader_never_falls_back_to_old_api(self):
        import vinted_api_light as bot
        http = SimpleNamespace(get=Mock())
        with self.assertRaisesRegex(CatalogUnavailable, 'non initialisé'):
            await bot.catalog_items('nintendo', None, 'https://www.vinted.be', AsyncMock(), http, {})
        http.get.assert_not_called()

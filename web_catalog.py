"""Lecteur expérimental de recherches publiques, sans URL d'API imposée.

Les schémas acceptés sont testés sur fixtures. Leur présence sur le site réel
reste à vérifier. Aucune note, date ou description n'est déduite du titre.
"""
import asyncio
import json
import logging
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit, parse_qs

LOGGER = logging.getLogger(__name__)


class CatalogUnavailable(RuntimeError):
    pass


class CatalogSession:
    def __init__(self, http, reader):
        self.http, self.catalog_reader = http, reader

    def __getattr__(self, name):
        return getattr(self.http, name)


def search_url(base_url, query, price_to=None, page=1):
    parts = urlsplit(base_url)
    if parts.scheme != 'https' or parts.netloc != 'www.vinted.be' or parts.path not in ('', '/'):
        raise ValueError('Le lecteur web attend https://www.vinted.be')
    params = {'search_text': query, 'order': 'newest_first', 'page': max(1, int(page))}
    if price_to is not None:
        params['price_to'] = float(price_to)
    return 'https://www.vinted.be/catalog?' + urlencode(params)


def catalog_payload(data, trusted_search_response=False):
    """None = schéma absent/invalide ; [] = catalogue explicitement vide."""
    def walk(node, catalog=False, depth=0):
        if depth > 12 or not isinstance(node, dict):
            return None
        if 'items' in node and catalog:
            values = node['items']
            if not isinstance(values, list):
                return None
            if all(isinstance(v, dict) and v.get('id') is not None
                   and isinstance(v.get('title'), str) and v.get('price') is not None
                   for v in values):
                return values
            return None
        for key, value in node.items():
            if isinstance(value, dict):
                found = walk(value, catalog or key.lower() in (
                    'catalog', 'catalogitems', 'catalog_items', 'searchresults', 'search_results'
                ), depth + 1)
                if found is not None:
                    return found
        return None
    return walk(data, trusted_search_response)


class JSONScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.chunks = []
        self.payloads = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script':
            self.active = attrs.get('type') == 'application/json'
            self.chunks = []

    def handle_data(self, data):
        if self.active:
            self.chunks.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.active:
            try:
                self.payloads.append(json.loads(''.join(self.chunks)))
            except (ValueError, TypeError):
                pass
            self.active = False


def embedded_catalog(html):
    parser = JSONScripts()
    parser.feed(html)
    for data in parser.payloads:
        items = catalog_payload(data)
        if items is not None:
            return items
    return None


def matches_search(url, query, page):
    parts = urlsplit(url)
    params = parse_qs(parts.query)
    return (parts.hostname == 'www.vinted.be'
            and params.get('search_text') == [query]
            and params.get('page', ['1']) == [str(page)])


class WebCatalog:
    def __init__(self, base_url, cfg):
        search_url(base_url, 'nintendo')
        self.base_url = base_url
        self.cfg = cfg
        self.stopped = None
        self.manager = self.browser = self.context = None
        self.limit = asyncio.Semaphore(max(1, min(int(cfg.get('web_max_concurrency', 2)), 3)))

    async def __aenter__(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise CatalogUnavailable('Lecteur web absent : installer les dépendances et Chromium avec le nouveau workflow.') from exc
        try:
            self.manager = await async_playwright().start()
            self.browser = await self.manager.chromium.launch(headless=True)
            self.context = await self.browser.new_context(locale='fr-BE')
        except Exception as exc:
            await self.__aexit__(None, None, None)
            raise CatalogUnavailable('Chromium ne démarre pas. Vérifier son installation dans le workflow.') from exc
        return self

    async def __aexit__(self, *args):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self.manager:
                await self.manager.stop()

    async def read(self, query, price_to, page, per_page, limiter, stats):
        async with self.limit:
            if self.stopped:
                raise CatalogUnavailable(self.stopped)
            return await self._read(query, price_to, page, per_page, limiter, stats)

    async def _read(self, query, price_to, page_number, per_page, limiter, stats):
        page = await self.context.new_page()
        pending, payloads = set(), []
        url = search_url(self.base_url, query, price_to, page_number)

        async def response_data(response):
            if not matches_search(response.url, query, page_number):
                return
            if response.request.resource_type not in ('xhr', 'fetch'):
                return
            await limiter.register_response(response.status, response.headers, 'catalog')
            if response.status in (401, 403, 429):
                self.stopped = f'Accès web refusé (HTTP {response.status}). Aucun contournement tenté.'
                return
            if response.status != 200 or 'json' not in response.headers.get('content-type', ''):
                return
            try:
                data = await response.json()
            except Exception:
                return  # Réponse interrompue/non JSON ; la page reste à contrôler.
            found = catalog_payload(data, trusted_search_response=True)
            if found is not None:
                payloads.append(found)

        def on_response(response):
            task = asyncio.create_task(response_data(response))
            pending.add(task)
            task.add_done_callback(pending.discard)
            # Récupérer les exceptions même si la réponse finit avant gather.
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        page.on('response', on_response)
        try:
            timeout = max(5000, min(int(self.cfg.get('web_timeout_ms', 20000)), 40000))
            response = await page.goto(url, wait_until='domcontentloaded', timeout=timeout)
            status = response.status if response else 0
            stats['catalog_last_status'] = status
            statuses = stats.setdefault('catalog_http_statuses', {})
            statuses[str(status)] = statuses.get(str(status), 0) + 1
            await limiter.register_response(status, response.headers if response else {}, 'catalog')
            if status in (401, 403, 429):
                self.stopped = f'Accès web refusé (HTTP {status}). Aucun contournement tenté.'
            if self.stopped:
                raise CatalogUnavailable(self.stopped)
            if status != 200:
                raise CatalogUnavailable(f'Recherche web indisponible (HTTP {status}) sur /catalog.')
            # Attente bornée du rendu et des réponses déclenchées par le site.
            try:
                await page.wait_for_load_state('networkidle', timeout=5000)
            except Exception:
                pass
            if pending:
                await asyncio.wait(tuple(pending), timeout=5)
            html = await page.content()
            body = (await page.locator('body').inner_text())[:20000].lower()
            if any(marker in body for marker in (
                'verify you are human', 'vérifiez que vous êtes humain',
                'access denied', 'unusual traffic', 'confirmez que vous êtes humain',
            )):
                self.stopped = 'Vérification humaine demandée. Lecture arrêtée.'
            if self.stopped:
                raise CatalogUnavailable(self.stopped)
            items = payloads[0] if payloads else embedded_catalog(html)
            if items is None:
                raise CatalogUnavailable(
                    'Page /catalog ouverte, mais aucune liste structurée compatible trouvée. '
                    'Le lecteur doit être adapté aux données réelles du site ; ce résultat ne signifie pas zéro annonce.'
                )
            LOGGER.info('Source WEB | page %s | %s articles structurés | notes et dates non inventées', page_number, len(items))
            return items[:max(1, min(int(per_page), 50))]
        except CatalogUnavailable:
            raise
        except Exception as exc:
            raise CatalogUnavailable('Lecture web interrompue : ' + type(exc).__name__) from exc
        finally:
            page.remove_listener('response', on_response)
            for task in tuple(pending):
                task.cancel()
            if pending:
                await asyncio.gather(*tuple(pending), return_exceptions=True)
            await page.close()

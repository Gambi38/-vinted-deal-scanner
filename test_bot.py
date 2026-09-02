import csv
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import playwright.async_api  # noqa: F401
except ModuleNotFoundError:
    playwright = types.ModuleType("playwright")
    async_api = types.ModuleType("playwright.async_api")
    async_api.async_playwright = object()
    async_api.TimeoutError = TimeoutError
    playwright.async_api = async_api
    sys.modules["playwright"] = playwright
    sys.modules["playwright.async_api"] = async_api

import fusion_donnees
import vinted_tarayici as bot


class EnvironmentConfigTests(unittest.TestCase):
    def test_env_file_parses_values_without_overwriting_real_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "export VINTED_TEST_NEW='depuis le fichier'\n"
                "VINTED_TEST_KEEP=depuis-env\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"VINTED_TEST_KEEP": "valeur-reelle"},
                clear=False,
            ):
                bot.load_env_file(env_file)
                self.assertEqual(os.environ["VINTED_TEST_NEW"], "depuis le fichier")
                self.assertEqual(os.environ["VINTED_TEST_KEEP"], "valeur-reelle")

    def test_environment_overrides_are_typed(self):
        cfg = {
            "base_url": "https://www.vinted.be",
            "reject_unknown_listing_age": True,
        }
        overrides = {
            "VINTED_MAX_LISTING_AGE_HOURS": "12",
            "VINTED_MAX_ITEMS_PER_SEARCH": "8",
            "VINTED_BACKOFF_MAX_SECONDS": "90",
            "VINTED_FRESHNESS_HEALTHCHECK_MIN_SAMPLES": "4",
            "VINTED_UNKNOWN_AGE_MAX_ATTEMPTS": "2",
            "VINTED_SEEN_RETENTION_DAYS": "30",
            "VINTED_REJECT_UNKNOWN_LISTING_AGE": "false",
            "VINTED_EXCLUDE_PRO_SELLERS": "false",
            "VINTED_PRICE_DROP_ALERT_PCT": "25",
        }
        with patch.dict(os.environ, overrides, clear=True):
            bot.apply_env_overrides(cfg)

        self.assertEqual(cfg["max_listing_age_hours"], 12.0)
        self.assertEqual(cfg["max_items_per_search"], 8)
        self.assertEqual(cfg["backoff_max_seconds"], 90.0)
        self.assertEqual(cfg["freshness_healthcheck_min_samples"], 4)
        self.assertEqual(cfg["unknown_age_max_attempts"], 2)
        self.assertEqual(cfg["seen_retention_days"], 30)
        self.assertFalse(cfg["reject_unknown_listing_age"])
        self.assertFalse(cfg["exclude_professional_sellers"])
        self.assertEqual(cfg["price_drop_alert_pct"], 25.0)

    def test_rate_limit_configuration_rejects_unsafe_values(self):
        base = {
            "base_url": "https://www.vinted.be",
            "request_delay_min_seconds": 1,
            "request_delay_max_seconds": 3,
            "backoff_max_seconds": 60,
            "startup_jitter_max_seconds": 20,
            "run_budget_seconds": 480,
            "max_listing_age_hours": 24,
            "max_items_per_search": 15,
        }
        bot.validate_runtime_config(dict(base))

        too_fast = dict(base, request_delay_min_seconds=0.1)
        with self.assertRaisesRegex(ValueError, ">= 0.5"):
            bot.validate_runtime_config(too_fast)

        reversed_range = dict(base, request_delay_min_seconds=3, request_delay_max_seconds=1)
        with self.assertRaisesRegex(ValueError, ">= au minimum"):
            bot.validate_runtime_config(reversed_range)

        broken_healthcheck = dict(base, freshness_healthcheck_min_samples=0)
        with self.assertRaisesRegex(ValueError, "healthcheck_min_samples"):
            bot.validate_runtime_config(broken_healthcheck)

        broken_attempts = dict(base, unknown_age_max_attempts=0)
        with self.assertRaisesRegex(ValueError, "unknown_age_max_attempts"):
            bot.validate_runtime_config(broken_attempts)

        broken_retention = dict(base, seen_retention_days=0)
        with self.assertRaisesRegex(ValueError, "seen_retention_days"):
            bot.validate_runtime_config(broken_retention)

        broken_price_drop = dict(base, price_drop_alert_pct=0)
        with self.assertRaisesRegex(ValueError, "price_drop_alert_pct"):
            bot.validate_runtime_config(broken_price_drop)


class RateLimiterBackoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_429_backoff_doubles_and_is_capped(self):
        limiter = bot.AsyncRateLimiter(1, 3, max_backoff_seconds=20)
        with self.assertLogs("vinted_tarayici", level="WARNING"):
            with patch.object(bot.time, "monotonic", return_value=100):
                first = await limiter.register_response(429, {}, "catalog")
                second = await limiter.register_response(
                    429,
                    {"Retry-After": "10"},
                    "catalog",
                )
                third = await limiter.register_response(429, {}, "catalog")

        self.assertEqual(first, 6)
        self.assertEqual(second, 12)
        self.assertEqual(third, 20)
        self.assertEqual(limiter._blocked_until, 120)

    async def test_success_resets_counter_after_cooldown(self):
        limiter = bot.AsyncRateLimiter(1, 3, max_backoff_seconds=20)
        with self.assertLogs("vinted_tarayici", level="WARNING"):
            with patch.object(bot.time, "monotonic", return_value=100):
                await limiter.register_response(429, {}, "catalog")
        with patch.object(bot.time, "monotonic", return_value=107):
            await limiter.register_response(200, {}, "catalog")
        self.assertEqual(limiter._consecutive_rate_limits, 0)

    def test_retry_after_parser_ignores_invalid_values(self):
        self.assertEqual(bot.retry_after_seconds({"Retry-After": "15"}), 15)
        self.assertIsNone(bot.retry_after_seconds({"Retry-After": "demain"}))

    def test_retry_after_parser_supports_an_http_date(self):
        now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            bot.retry_after_seconds(
                {"Retry-After": "Wed, 02 Sep 2026 12:00:30 GMT"},
                now=now,
            ),
            30,
        )

    async def test_server_retry_after_is_honored_even_above_local_cap(self):
        limiter = bot.AsyncRateLimiter(1, 3, max_backoff_seconds=20)
        with self.assertLogs("vinted_tarayici", level="WARNING"):
            with patch.object(bot.time, "monotonic", return_value=100):
                delay = await limiter.register_response(
                    429,
                    {"Retry-After": "120"},
                    "catalog",
                )
        self.assertEqual(delay, 120)
        self.assertEqual(limiter._blocked_until, 220)

    async def test_detail_http_429_is_reported_for_retry(self):
        class Response:
            status = 429
            headers = {"retry-after": "8"}

        class DetailPage:
            closed = False

            async def goto(self, *args, **kwargs):
                return Response()

            async def close(self):
                self.closed = True

        detail_page = DetailPage()

        class Context:
            async def new_page(self):
                return detail_page

        class Page:
            context = Context()

        with self.assertLogs("vinted_tarayici", level="WARNING"):
            result = await bot.verify_listing(
                Page(),
                "https://www.vinted.be/items/123-test",
                "Test",
                limiter=bot.AsyncRateLimiter(1, 3, 60),
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["rate_limited"])
        self.assertIn("429", result["error"])
        self.assertTrue(detail_page.closed)

    async def test_catalog_http_429_stops_search_and_sets_backoff(self):
        class Response:
            status = 429
            headers = {"retry-after": "9"}

        class Page:
            def on(self, *args):
                pass

            def remove_listener(self, *args):
                pass

            async def goto(self, *args, **kwargs):
                return Response()

        limiter = bot.AsyncRateLimiter(1, 3, 60)
        with self.assertLogs("vinted_tarayici", level="WARNING"):
            result = await bot.scan_search(
                Page(),
                {"name": "Test", "query": "switch", "rules": []},
                {"base_url": "https://www.vinted.be"},
                {},
                set(),
                limiter,
            )

        self.assertEqual(result, [])
        self.assertEqual(limiter._consecutive_rate_limits, 1)


class FilteringAndScoringTests(unittest.TestCase):
    def test_rule_match_requires_keywords_and_honors_exclusions(self):
        rule = {
            "must_contain": ["switch"],
            "exclude": ["pochette"],
        }
        self.assertTrue(bot.rule_match(rule, "Nintendo Switch OLED", "console"))
        self.assertFalse(bot.rule_match(rule, "Nintendo OLED", "console"))
        self.assertFalse(
            bot.rule_match(rule, "Nintendo Switch OLED", "vendue avec pochette")
        )

    def test_score_candidate_uses_configured_fees(self):
        rule = {"resale_low": 60, "resale_high": 70}
        cfg = {
            "shipping_estimate": 4.5,
            "buyer_protection_estimate": {"fixed": 0.7, "pct": 0.05},
        }
        total, low, high, margin_low, margin_high, roi = bot.score_candidate(
            rule,
            20,
            cfg,
        )
        self.assertEqual((total, low, high), (26.2, 60.0, 70.0))
        self.assertEqual((margin_low, margin_high), (33.8, 43.8))
        self.assertEqual(roi, 129.0)

    def test_favourites_distinguish_a_hidden_deal_from_visible_competition(self):
        cfg = {
            "hidden_deal_bonus": 1,
            "favourite_penalty_per_user": 0.25,
            "favourite_penalty_cap": 2,
        }
        hidden = bot.opportunity_score(
            30,
            100,
            50,
            [],
            age_hours=2 / 60,
            favourite_count=0,
            cfg=cfg,
        )
        contested = bot.opportunity_score(
            30,
            100,
            50,
            [],
            age_hours=2 / 60,
            favourite_count=8,
            cfg=cfg,
        )
        self.assertGreater(hidden, contested)

    def test_blacklist_blocks_accessories_and_reports_risk_words(self):
        blacklist = {
            "title_accessory_blacklist": ["boite"],
            "hard_blacklist": [],
            "fake_blacklist": [],
            "accessory_blacklist": ["chargeur"],
            "suspicious_words": ["urgent"],
        }
        blocked, group, hits, risks = bot.blacklist_check(
            "Boîte Nintendo Switch vide",
            "sans console",
            blacklist,
        )
        self.assertTrue(blocked)
        self.assertEqual(group, "title_accessory_blacklist")
        self.assertEqual(hits, ["boite"])
        self.assertEqual(risks, [])

        blocked, group, hits, risks = bot.blacklist_check(
            "Nintendo Switch OLED",
            "urgent, vente rapide",
            blacklist,
        )
        self.assertFalse(blocked)
        self.assertEqual((group, hits), ("", []))
        self.assertEqual(risks, ["urgent"])


class CatalogSignalTests(unittest.IsolatedAsyncioTestCase):
    def test_current_catalog_payload_exposes_favourites_and_business_status(self):
        item = {
            "id": 9865091920,
            "productItem": {
                "id": 9865091920,
                "title": "Switch Nintendo",
                "url": "/items/9865091920-switch-nintendo",
                "favouriteCount": 3,
                "price": {"amount": "125.00", "currencyCode": "EUR"},
                "user": {"isBusiness": False},
                "thumbnailUrl": "https://example.test/item.webp",
            },
            "itemBox": {
                "firstLine": "Nintendo",
                "secondLine": "Très bon état",
            },
        }

        card = bot.catalog_card_from_item(item)

        self.assertEqual(card["item_id"], "9865091920")
        self.assertEqual(card["price"], 125.0)
        self.assertEqual(card["favourite_count"], 3)
        self.assertIs(card["seller_is_business"], False)
        self.assertIn("Nintendo", card["text"])
        self.assertEqual(
            card["url"],
            "https://www.vinted.be/items/9865091920-switch-nintendo",
        )

    def test_snake_case_catalog_payload_is_also_supported(self):
        card = bot.catalog_card_from_item({
            "id": 123,
            "title": "Console",
            "url": "/items/123-console",
            "price": {"amount": "20"},
            "favourite_count": 2,
            "user": {"is_business": True},
        })
        self.assertEqual(card["favourite_count"], 2)
        self.assertIs(card["seller_is_business"], True)

    async def test_api_fast_path_does_not_wait_for_the_visual_grid(self):
        class Page:
            def locator(self, selector):
                raise AssertionError("Le DOM ne doit pas être lu avec le payload API")

        cards = await bot.extract_cards(
            Page(),
            catalog_payload={
                "items": [{
                    "id": 123,
                    "title": "Nintendo Switch",
                    "url": "/items/123-switch",
                    "price": {"amount": "20"},
                    "favourite_count": 0,
                    "user": {"is_business": False},
                }]
            },
        )
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["price"], 20.0)


class SeenStateTests(unittest.TestCase):
    def test_rejection_is_scoped_to_one_search(self):
        first = {"name": "Nintendo Switch"}
        second = {"name": "Switch OLED"}
        state = {bot.search_seen_key(first, "123")}

        self.assertTrue(bot.item_already_seen(state, first, "123"))
        self.assertFalse(bot.item_already_seen(state, second, "123"))

    def test_alert_marker_deduplicates_all_searches(self):
        state = {bot.alert_seen_key("123")}
        self.assertTrue(bot.item_already_seen(state, {"name": "A"}, "123"))
        self.assertTrue(bot.item_already_seen(state, {"name": "B"}, "123"))

    def test_legacy_ids_remain_supported(self):
        self.assertTrue(bot.item_already_seen({"123"}, {"name": "A"}, "123"))

    def test_unknown_age_retries_stop_after_two_attempts(self):
        search = {"name": "Nintendo Switch"}
        state = set()
        metadata = {}

        first, retry_first = bot.register_unknown_age_attempt(
            state,
            search,
            "123",
            max_attempts=2,
            seen_meta=metadata,
        )
        second, retry_second = bot.register_unknown_age_attempt(
            state,
            search,
            "123",
            max_attempts=2,
            seen_meta=metadata,
        )

        self.assertEqual((first, retry_first), (1, True))
        self.assertEqual((second, retry_second), (2, False))
        self.assertFalse(
            any(str(key).startswith(bot.FRESHNESS_RETRY_PREFIX) for key in state)
        )

    def test_seen_state_prunes_old_keys_and_dates_legacy_entries(self):
        now = 1_800_000_000.0
        state = {"old", "recent", "legacy"}
        metadata = {
            "old": now - 31 * 86400,
            "recent": now - 29 * 86400,
            "orphan": now,
        }

        removed = bot.prune_seen_state(
            state,
            metadata,
            retention_days=30,
            now=now,
        )

        self.assertEqual(removed, 1)
        self.assertEqual(state, {"recent", "legacy"})
        self.assertEqual(metadata["legacy"], now)
        self.assertNotIn("orphan", metadata)

    def test_seen_state_saves_compatible_list_and_timestamp_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seen_path = root / "annonces_vues.json"
            meta_path = root / "annonces_vues_meta.json"
            with patch.object(bot, "SEEN_PATH", seen_path), patch.object(
                bot,
                "SEEN_META_PATH",
                meta_path,
            ):
                bot.save_seen_state({"b", "a"}, {"a": 10, "b": 20})

            self.assertEqual(json.loads(seen_path.read_text()), ["a", "b"])
            self.assertEqual(
                json.loads(meta_path.read_text()),
                {"a": 10.0, "b": 20.0},
            )


class PriceHistoryTests(unittest.TestCase):
    def test_cumulative_drop_triggers_at_twenty_percent(self):
        history = {}
        bot.remember_price(history, "123", 50, now=100)
        bot.remember_price(history, "123", 46, now=110)
        bot.remember_price(history, "123", 42, now=120)

        self.assertIsNone(bot.price_drop_event(history, "123", 41, 20))
        event = bot.price_drop_event(history, "123", 40, 20)
        self.assertEqual(event["previous_price"], 50.0)
        self.assertEqual(event["current_price"], 40.0)
        self.assertEqual(event["price_drop_pct"], 20.0)

    def test_alerted_drop_resets_the_baseline(self):
        history = {
            "123": {
                "baseline_price": 50,
                "last_price": 50,
                "seen_at": 100,
            }
        }
        self.assertIsNotNone(bot.price_drop_event(history, "123", 35, 20))
        bot.remember_price(
            history,
            "123",
            35,
            now=200,
            reset_baseline=True,
        )
        self.assertIsNone(bot.price_drop_event(history, "123", 35, 20))
        self.assertEqual(history["123"]["baseline_price"], 35.0)

    def test_price_history_is_pruned_and_saved_separately(self):
        now = 1_800_000_000.0
        history = {
            "old": {"baseline_price": 50, "last_price": 50, "seen_at": now - 31 * 86400},
            "recent": {"baseline_price": 20, "last_price": 20, "seen_at": now},
        }
        removed = bot.prune_price_history(history, retention_days=30, now=now)
        self.assertEqual(removed, 1)
        self.assertEqual(set(history), {"recent"})

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "annonces_prix.json"
            with patch.object(bot, "PRICE_HISTORY_PATH", output):
                bot.save_price_history(history)
            self.assertEqual(json.loads(output.read_text()), history)


class CatalogScanSignalTests(unittest.IsolatedAsyncioTestCase):
    class Limiter:
        async def wait(self, request_kind):
            return None

        async def register_response(self, *args, **kwargs):
            return 0.0

    @staticmethod
    def page_for(item):
        class Response:
            status = 200
            headers = {}
            url = "https://www.vinted.be/api/v2/catalog/items"

            async def json(self):
                return {"items": [item]}

        class Page:
            callback = None

            def on(self, event, callback):
                self.callback = callback

            def remove_listener(self, event, callback):
                self.callback = None

            async def goto(self, *args, **kwargs):
                response = Response()
                await self.callback(response)
                return response

        return Page()

    async def test_professional_seller_is_rejected_before_detail_request(self):
        item = {
            "id": 123,
            "title": "Nintendo Switch console",
            "url": "/items/123-switch",
            "price": {"amount": "20"},
            "user": {"is_business": True},
        }
        detail_called = False

        async def detail(*args, **kwargs):
            nonlocal detail_called
            detail_called = True
            return {"ok": True}

        with patch.object(bot, "verify_listing", side_effect=detail):
            alerts = await bot.scan_search(
                self.page_for(item),
                {"name": "Switch", "query": "switch", "rules": []},
                {
                    "base_url": "https://www.vinted.be",
                    "exclude_professional_sellers": True,
                },
                {},
                set(),
                self.Limiter(),
            )

        self.assertEqual(alerts, [])
        self.assertFalse(detail_called)

    async def test_twenty_percent_drop_rechecks_an_already_seen_listing(self):
        item = {
            "id": 123,
            "title": "Nintendo Switch console OLED",
            "url": "/items/123-switch",
            "price": {"amount": "35"},
            "favourite_count": 0,
            "user": {"is_business": False},
        }
        search = {
            "name": "Switch",
            "query": "switch",
            "category": "CONSOLE",
            "price_to": 50,
            "rules": [{
                "label": "Switch OLED",
                "must_contain": ["switch"],
                "platform_any": ["switch"],
                "resale_low": 100,
                "resale_high": 110,
                "max_buy_ratio": 1,
                "min_margin": 0,
                "min_roi_pct": 0,
                "demand_score": 5,
            }],
        }
        cfg = {
            "base_url": "https://www.vinted.be",
            "max_items_per_search": 10,
            "max_listing_age_hours": 24,
            "reject_unknown_listing_age": True,
            "exclude_professional_sellers": True,
            "price_drop_alert_pct": 20,
            "shipping_estimate": 0,
            "buyer_protection_estimate": {"fixed": 0, "pct": 0},
            "min_margin": 0,
            "min_roi_pct": 0,
            "min_demand_score": 0,
        }
        seen = {
            bot.search_seen_key(search, "123"),
            bot.alert_seen_key("123"),
        }
        history = {
            "123": {
                "baseline_price": 50,
                "last_price": 50,
                "seen_at": 100,
            }
        }

        async def detail(*args, **kwargs):
            return {
                "ok": True,
                "available": True,
                "title": "Nintendo Switch console OLED",
                "text": "Nintendo Switch console OLED",
                "seller": "vendeur",
                "seller_is_business": False,
                "favourite_count": 0,
                "image_url": "",
                "price": 35.0,
                "created_at_ts": "Ajouté il y a 2 minutes",
            }

        with patch.object(bot, "verify_listing", side_effect=detail), patch.object(
            bot,
            "append_alert",
        ), patch.object(bot, "ntfy_send"):
            alerts = await bot.scan_search(
                self.page_for(item),
                search,
                cfg,
                {},
                seen,
                self.Limiter(),
                price_history=history,
                price_drop_cache={},
            )

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["previous_price"], 50.0)
        self.assertEqual(alerts[0]["price_drop_pct"], 30.0)
        self.assertIn("prix baisse", alerts[0]["reason"])
        self.assertEqual(history["123"]["baseline_price"], 35.0)


class FreshnessSeenStateTests(unittest.IsolatedAsyncioTestCase):
    async def _scan_with_age(self, created_at_value):
        class Response:
            status = 200
            headers = {}
            url = "https://www.vinted.be/api/v2/catalog/items"

            async def json(self):
                return {"items": [{"id": 123}]}

        class Locator:
            @property
            def first(self):
                return self

            async def wait_for(self, **kwargs):
                return None

        class Page:
            callback = None

            def on(self, event, callback):
                self.callback = callback

            def remove_listener(self, event, callback):
                self.callback = None

            async def goto(self, *args, **kwargs):
                await self.callback(Response())
                return Response()

            async def wait_for_timeout(self, milliseconds):
                return None

            def locator(self, selector):
                return Locator()

        class Limiter:
            async def wait(self, request_kind):
                return None

            async def register_response(self, *args, **kwargs):
                return 0.0

        async def cards(*args, **kwargs):
            return [{
                "item_id": "123",
                "url": "https://www.vinted.be/items/123-test",
                "title": "Nintendo Switch console OLED",
                "text": "Nintendo Switch console OLED 20 €",
                "created_at_ts": None,
            }]

        async def detail(*args, **kwargs):
            return {
                "ok": True,
                "available": True,
                "title": "Nintendo Switch console OLED",
                "text": "Nintendo Switch console OLED",
                "seller": "vendeur",
                "image_url": "",
                "price": 20.0,
                "created_at_ts": created_at_value,
            }

        search = {
            "name": "Test Switch",
            "query": "switch",
            "category": "CONSOLE",
            "price_to": 50,
            "rules": [{
                "label": "Switch OLED",
                "must_contain": ["switch"],
                "platform_any": ["switch"],
                "resale_low": 100,
                "resale_high": 110,
                "max_buy_ratio": 1,
                "min_margin": 0,
                "min_roi_pct": 0,
                "demand_score": 5,
            }],
        }
        cfg = {
            "base_url": "https://www.vinted.be",
            "page_wait_ms": 0,
            "max_items_per_search": 1,
            "max_listing_age_hours": 24,
            "reject_unknown_listing_age": True,
            "shipping_estimate": 0,
            "buyer_protection_estimate": {"fixed": 0, "pct": 0},
            "min_margin": 0,
            "min_roi_pct": 0,
            "min_demand_score": 0,
        }
        seen = set()
        with patch.object(bot, "extract_cards", side_effect=cards), patch.object(
            bot,
            "verify_listing",
            side_effect=detail,
        ):
            alerts = await bot.scan_search(
                Page(),
                search,
                cfg,
                {},
                seen,
                Limiter(),
            )
        return alerts, seen, search

    async def test_unknown_age_is_retried_instead_of_becoming_permanently_seen(self):
        alerts, seen, search = await self._scan_with_age(None)
        self.assertEqual(alerts, [])
        self.assertNotIn(bot.search_seen_key(search, "123"), seen)

    async def test_known_old_listing_remains_seen_after_strict_rejection(self):
        alerts, seen, search = await self._scan_with_age("Ajouté il y a 2 jours")
        self.assertEqual(alerts, [])
        self.assertIn(bot.search_seen_key(search, "123"), seen)


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        self.cfg = {
            "max_listing_age_hours": 24,
            "reject_unknown_listing_age": True,
            "instant_listing_minutes": 5,
        }

    def test_parses_unix_seconds_milliseconds_and_iso(self):
        expected = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
        seconds = expected.timestamp()
        self.assertEqual(bot.parse_vinted_timestamp(seconds), expected)
        self.assertEqual(bot.parse_vinted_timestamp(seconds * 1000), expected)
        self.assertEqual(
            bot.parse_vinted_timestamp("2026-09-02T10:00:00Z"),
            expected,
        )

    def test_browser_locale_and_http_language_are_forced_to_french(self):
        self.assertEqual(bot.VINTED_BROWSER_LOCALE, "fr-BE")
        self.assertTrue(bot.VINTED_ACCEPT_LANGUAGE.startswith("fr-BE,fr"))

    def test_accepts_24_hours_and_rejects_older(self):
        exactly_24h = self.now - timedelta(hours=24)
        older = self.now - timedelta(hours=24, seconds=1)
        self.assertTrue(bot.freshness_check(exactly_24h.isoformat(), self.cfg, self.now)[0])
        self.assertFalse(bot.freshness_check(older.isoformat(), self.cfg, self.now)[0])

    def test_unknown_age_is_rejected_in_strict_mode(self):
        accepted, age, reason = bot.freshness_check(None, self.cfg, self.now)
        self.assertFalse(accepted)
        self.assertIsNone(age)
        self.assertIn("introuvable", reason)

    def test_current_vinted_relative_age_is_parsed_without_seller_confusion(self):
        live_page_text = (
            "Très bon état·Nintendo·Ajouté il y a une minute\n"
            "Ajouté\nIl y a une minute\n"
            "Vu la dernière fois : il y a une heure"
        )
        accepted, age, reason = bot.freshness_check(
            live_page_text,
            self.cfg,
            self.now,
        )

        self.assertTrue(accepted)
        self.assertAlmostEqual(age, 1 / 60)
        self.assertEqual(reason, "annonce récente")
        self.assertIsNone(
            bot.relative_listing_age_bounds(
                "Vu la dernière fois : il y a une minute"
            )
        )

    def test_relative_age_uses_a_conservative_24_hour_boundary(self):
        self.assertTrue(
            bot.freshness_check(
                "Ajouté il y a 23 heures",
                self.cfg,
                self.now,
            )[0]
        )
        self.assertFalse(
            bot.freshness_check(
                "Ajouté il y a 24 heures",
                self.cfg,
                self.now,
            )[0]
        )
        self.assertFalse(
            bot.freshness_check(
                "Ajouté\nIl y a 2 jours",
                self.cfg,
                self.now,
            )[0]
        )

    def test_relative_instant_and_less_than_one_minute_are_accepted(self):
        for label in (
            "Ajouté à l'instant",
            "Ajouté il y a moins d'une minute",
            "Ajouté il y a quelques secondes",
        ):
            with self.subTest(label=label):
                accepted, age, _ = bot.freshness_check(label, self.cfg, self.now)
                self.assertTrue(accepted)
                self.assertLess(age, 1 / 60 + 1e-9)

    def test_abbreviated_french_and_english_age_formats_are_supported(self):
        expected = {
            "Ajouté il y a 2 min": 2 / 60,
            "Ajouté il y a 1 h": 1,
            "Added 4 minutes ago": 4 / 60,
            "Uploaded one hour ago": 1,
        }
        for label, expected_age in expected.items():
            with self.subTest(label=label):
                accepted, age, _ = bot.freshness_check(label, self.cfg, self.now)
                self.assertTrue(accepted)
                self.assertAlmostEqual(age, expected_age)

        self.assertFalse(
            bot.freshness_check("Added yesterday", self.cfg, self.now)[0]
        )

    def test_under_five_minutes_gets_the_equivalent_of_twenty_points(self):
        base = bot.opportunity_score(50, 100, 20, [], age_hours=8)
        thirty_minutes = bot.opportunity_score(50, 100, 20, [], age_hours=0.4)
        under_five = bot.opportunity_score(50, 100, 20, [], age_hours=4 / 60)
        self.assertEqual(thirty_minutes, base + 1)
        self.assertEqual(under_five, base + 2)

    def test_healthcheck_detects_only_a_systemic_age_parser_outage(self):
        health_cfg = {"freshness_healthcheck_min_samples": 3}
        self.assertFalse(
            bot.freshness_health_issue(
                {"freshness_known": 0, "freshness_unknown": 2},
                health_cfg,
            )
        )
        self.assertTrue(
            bot.freshness_health_issue(
                {"freshness_known": 0, "freshness_unknown": 3},
                health_cfg,
            )
        )
        self.assertTrue(
            bot.freshness_health_issue(
                {"freshness_known": 1, "freshness_unknown": 20},
                health_cfg,
            )
        )
        self.assertFalse(
            bot.freshness_health_issue(
                {"freshness_known": 3, "freshness_unknown": 2},
                health_cfg,
            )
        )

    def test_search_healthcheck_returns_a_nonzero_failure_only_when_systemic(self):
        self.assertFalse(bot.search_health_issue(2, 2))
        self.assertFalse(bot.search_health_issue(10, 4))
        self.assertTrue(bot.search_health_issue(10, 5))

    def test_instant_listing_gets_a_score_bonus(self):
        base = bot.opportunity_score(25, 100, 50, [], age_hours=8)
        instant = bot.opportunity_score(25, 100, 50, [], age_hours=0.2)
        self.assertGreater(instant, base)

    def test_catalog_payload_variants(self):
        item = {"id": 123, "created_at_ts": 1}
        self.assertEqual(bot.catalog_items_from_payload({"items": [item]}), [item])
        self.assertEqual(
            bot.catalog_items_from_payload({"data": {"items": [item]}}),
            [item],
        )

    def test_search_url_uses_the_current_newest_first_parameter(self):
        url = bot.build_search_url(
            {"query": "nintendo switch", "price_to": 30},
            {"base_url": "https://www.vinted.be"},
        )
        self.assertIn("search_text=nintendo+switch", url)
        self.assertIn("order=newest_first", url)
        self.assertIn("price_to=30", url)
        self.assertNotIn("sort_by=created_at_desc", url)


class CsvTests(unittest.TestCase):
    def test_alert_csv_keeps_risk_demand_and_image(self):
        row = {
            "item_id": "123",
            "image_url": "https://example.test/image.jpg",
            "demand_score": 5,
            "risk": "prix anormalement bas",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "alertes.csv"
            with patch.object(bot, "ALERTS_CSV", output):
                bot.append_alert(row)
            with output.open(encoding="utf-8-sig", newline="") as handle:
                saved = next(csv.DictReader(handle))

        self.assertEqual(saved["image_url"], row["image_url"])
        self.assertEqual(saved["demand_score"], "5")
        self.assertEqual(saved["risk"], row["risk"])

    def test_old_alert_csv_is_upgraded_before_append(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "alertes.csv"
            output.write_text(
                "timestamp,title,url,item_id\n2026-01-01,ancienne,https://old,1\n",
                encoding="utf-8",
            )
            with patch.object(bot, "ALERTS_CSV", output):
                bot.append_alert({
                    "title": "nouvelle",
                    "image_url": "https://image",
                    "demand_score": 5,
                    "risk": "aucun",
                    "url": "https://new",
                    "item_id": "2",
                })
            with output.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)

        self.assertEqual(reader.fieldnames, bot.ALERT_FIELDS)
        self.assertEqual(rows[0]["title"], "ancienne")
        self.assertEqual(rows[1]["risk"], "aucun")

    def test_merge_preserves_union_of_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote.csv"
            local = root / "local.csv"
            output = root / "output.csv"
            remote.write_text("item_id,title\n1,ancien\n", encoding="utf-8")
            local.write_text("item_id,title,risk\n2,nouveau,doute\n", encoding="utf-8")

            fusion_donnees.fusionner_alertes(remote, local, output)
            with output.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)

        self.assertEqual(reader.fieldnames, ["item_id", "title", "risk"])
        self.assertEqual(rows[1]["risk"], "doute")


if __name__ == "__main__":
    unittest.main()

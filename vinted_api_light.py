#!/usr/bin/env python3
# VINTED_API_LIGHT_V1
# Version de test : utilise UNIQUEMENT l'API HTTP (aiohttp) pour le catalogue et les détails.
# Ce fichier est indépendant de vinted_tarayici.py et ne le modifie pas.

import asyncio
import aiohttp
import csv
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# ---------- Configuration ----------
ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("VINTED_DATA_DIR", str(ROOT / "runtime_data"))).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = ROOT / "config.json"
BLACKLIST_PATH = ROOT / "blacklist.json"
SEEN_PATH = DATA_DIR / "annonces_vues.json"
SEEN_META_PATH = DATA_DIR / "annonces_vues_meta.json"
PRICE_HISTORY_PATH = DATA_DIR / "annonces_prix.json"
ALERTS_CSV = DATA_DIR / "alertes.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
LOGGER = logging.getLogger("vinted_api_light")

# ---------- Utilitaires ----------
def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)

def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()

def term_present(text, term):
    t = norm(text)
    nt = norm(term)
    if not nt:
        return False
    return re.search(rf"(?<!\w){re.escape(nt)}(?!\w)", t) is not None

def _positive_price(value):
    if isinstance(value, dict):
        value = value.get("amount") or value.get("value") or value.get("price")
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None

def parse_vinted_timestamp(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:
            value /= 1000.0
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, ValueError):
            return None
    if isinstance(value, str):
        raw = value.strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            return parse_vinted_timestamp(float(raw))
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None

def listing_age_hours(value, now=None):
    dt = parse_vinted_timestamp(value)
    if dt is not None:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return max(0.0, (current - dt).total_seconds() / 3600.0)
    return None

def freshness_check(value, cfg, now=None):
    max_age = float(cfg.get("max_listing_age_hours", 24))
    age = listing_age_hours(value, now)
    if age is None:
        if cfg.get("reject_unknown_listing_age", True):
            return False, None, "âge inconnu"
        return True, None, "âge inconnu toléré"
    if age > max_age:
        return False, age, f"trop ancienne ({age:.1f}h > {max_age:.1f}h)"
    return True, age, "récente"

def freshness_label(age_hours, cfg):
    if age_hours is None:
        return "âge inconnu"
    minutes = int(round(age_hours * 60))
    if minutes <= cfg.get("instant_listing_minutes", 5):
        return f"Mise à l'instant ({minutes} min)"
    if minutes < 60:
        return f"publiée il y a {minutes} min"
    return f"publiée il y a {age_hours:.1f}h"

# ---------- Rate Limiter ----------
class AsyncRateLimiter:
    def __init__(self, min_sec, max_sec, max_backoff=60):
        self.min = float(min_sec)
        self.max = float(max_sec)
        self.max_backoff = float(max_backoff)
        self._next_allowed = 0.0
        self._blocked_until = 0.0
        self._consecutive = 0
        self._lock = asyncio.Lock()

    async def wait(self, kind="request"):
        async with self._lock:
            now = time.monotonic()
            wait_until = max(self._next_allowed, self._blocked_until)
            remaining = wait_until - now
            if remaining > 0:
                await asyncio.sleep(remaining)
            delay = random.uniform(self.min, self.max)
            self._next_allowed = time.monotonic() + delay

    async def register_response(self, status, headers=None, kind="request"):
        try:
            status = int(status)
        except (TypeError, ValueError):
            return 0.0
        async with self._lock:
            now = time.monotonic()
            if status == 429:
                self._consecutive += 1
                exp = self.max * (2 ** self._consecutive)
                retry_after = self._parse_retry_after(headers) or 0.0
                delay = max(min(self.max_backoff, exp), retry_after)
                self._blocked_until = max(self._blocked_until, now + delay)
                LOGGER.warning("HTTP 429 | %s | backoff %.1f s", kind, delay)
                return delay
            if 200 <= status < 400 and now >= self._blocked_until:
                self._consecutive = 0
            return 0.0

    @staticmethod
    def _parse_retry_after(headers):
        if not headers:
            return None
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw is None:
            return None
        try:
            return max(0.0, float(str(raw).strip()))
        except ValueError:
            return None

# ---------- Gestion état ----------
def search_seen_key(search, item_id):
    name = norm(str(search.get("name") or search.get("query") or "search"))
    return f"search::{name}::{item_id}"

def alert_seen_key(item_id):
    return f"alert::{item_id}"

def mark_seen(seen_ids, key, seen_meta=None, now=None):
    key = str(key)
    seen_ids.add(key)
    if seen_meta is not None:
        seen_meta[key] = float(now if now is not None else time.time())

def item_already_seen(seen_ids, search, item_id):
    return (str(item_id) in seen_ids or
            search_seen_key(search, item_id) in seen_ids or
            alert_seen_key(item_id) in seen_ids)

def prune_seen_state(seen_ids, seen_meta, retention_days=30, now=None):
    current = float(now if now is not None else time.time())
    cutoff = current - float(retention_days) * 86400
    removed = 0
    for key in list(seen_ids):
        ts = seen_meta.get(str(key)) if seen_meta else None
        if ts is None:
            if seen_meta is not None:
                seen_meta[str(key)] = current
            continue
        try:
            t = float(ts)
        except (TypeError, ValueError):
            t = current
        if t < cutoff:
            seen_ids.discard(key)
            if seen_meta:
                seen_meta.pop(str(key), None)
            removed += 1
    return removed

def save_seen_state(seen_ids, seen_meta):
    keys = sorted(str(k) for k in seen_ids)
    save_json(SEEN_PATH, keys)
    meta = {k: float(seen_meta.get(k, time.time())) for k in keys}
    save_json(SEEN_META_PATH, meta)

# ---------- Scoring ----------
def fee_estimate(price, cfg):
    bp = cfg.get("buyer_protection_estimate", {})
    return (float(bp.get("fixed", 0.70)) +
            float(bp.get("pct", 0.05)) * price +
            float(cfg.get("shipping_estimate", 4.50)))

def score_candidate(rule, price, cfg):
    total = price + fee_estimate(price, cfg)
    low = rule.get("resale_low")
    high = rule.get("resale_high", low)
    if low is None:
        return total, None, None, None, None, None
    low = float(low)
    high = float(high or low)
    margin_low = low - total
    margin_high = high - total
    roi = (margin_low / total * 100) if total > 0 else 0
    return total, low, high, round(margin_low, 2), round(margin_high, 2), round(roi, 1)

def opportunity_score(price, ref_price, margin_low, motivation_hits, age_hours=None,
                      favourite_count=None, cfg=None):
    if not ref_price or ref_price <= 0:
        return 1
    ratio = price / ref_price
    if ratio <= 0.12:
        score = 10
    elif ratio <= 0.15:
        score = 9
    elif ratio <= 0.20:
        score = 8
    elif ratio <= 0.25:
        score = 7
    elif ratio <= 0.30:
        score = 6
    elif ratio <= 0.40:
        score = 5
    else:
        score = 3
    if margin_low >= 100:
        score += 1
    elif margin_low >= 70:
        score += 0.5
    if motivation_hits:
        score += 1
    if age_hours is not None:
        if age_hours <= 5/60:
            score += 2
        elif age_hours <= 0.5:
            score += 1
        elif age_hours <= 2:
            score += 0.5
    if favourite_count is not None:
        favs = max(0, int(favourite_count)) if favourite_count is not None else 0
        if favs == 0 and age_hours is not None and age_hours <= 5/60:
            score += float(cfg.get("hidden_deal_bonus", 1.0))
        else:
            penalty = favs * float(cfg.get("favourite_penalty_per_user", 0.25))
            score -= min(penalty, float(cfg.get("favourite_penalty_cap", 2.0)))
    return max(1, min(10, int(round(score))))

# ---------- Filtres simplifiés ----------
def blacklist_check(title, text, blacklist):
    combined = norm(f"{title} {text}")
    for group in ("hard_blacklist", "fake_blacklist", "accessory_blacklist", "title_accessory_blacklist"):
        if group == "title_accessory_blacklist":
            hits = [w for w in blacklist.get(group, []) if term_present(title, w)]
        else:
            hits = [w for w in blacklist.get(group, []) if term_present(combined, w)]
        if hits:
            return True, group, hits[:3], []
    risks = [w for w in blacklist.get("suspicious_words", []) if term_present(combined, w)]
    return False, "", [], risks[:3]

def category_sanity_check(category, title):
    if category.startswith("JEU_"):
        merch = ["steelbook", "pin", "badge", "figurine", "amiibo", "poster", "artbook", "guide", "boite vide", "empty box"]
        if any(term_present(title, w) for w in merch):
            return False, "objet dérivé/accessoire"
    elif category == "CONSOLE":
        accessoires = ["chargeur", "manette", "dock", "câble", "alimentation", "batterie", "coque"]
        if any(term_present(title, w) for w in accessoires):
            return False, "accessoire console"
        if any(term_present(title, w) for w in ["jeu", "game"]) and not any(term_present(title, w) for w in ["console", "avec"]):
            return False, "annonce de jeu, pas console"
    return True, ""

def empty_packaging_check(category, title, text):
    if not category.startswith("JEU_"):
        return False, []
    phrases = ["boite vide", "boîte vide", "boitier vide", "sans jeu", "empty box", "box only"]
    combined = norm(f"{title} {text}")
    hits = [p for p in phrases if term_present(combined, p)]
    if hits:
        return True, hits[:3]
    return False, []

def rule_match(rule, title, text, deep=False):
    title_n = norm(title)
    full = norm(f"{title} {text}")
    must = rule.get("must_contain", [])
    any_kw = rule.get("any_contain", [])
    exclude = rule.get("exclude", [])
    if must and not all(term_present(title_n, w) for w in must):
        return False
    if any_kw and not any(term_present(title_n, w) for w in any_kw):
        return False
    if exclude and any(term_present(full, w) for w in exclude):
        return False
    if not deep:
        return True
    platform = rule.get("platform_any", [])
    hardware = rule.get("hardware_any", [])
    if platform and not any(term_present(full, w) for w in platform):
        return False
    if hardware:
        hardware_text = title_n if rule.get("hardware_in_title") else full
        if not any(term_present(hardware_text, w) for w in hardware):
            return False
    return True

# ---------- Appels API ----------
async def catalog_items(query, price_to, base_url, limiter, session, headers):
    url = f"{base_url}/api/v2/catalog/items"
    params = {"search_text": query, "order": "newest_first", "per_page": 40}
    if price_to is not None:
        params["price_to"] = float(price_to)
    await limiter.wait("catalog")
    try:
        async with session.get(url, params=params, headers=headers, timeout=10) as resp:
            await limiter.register_response(resp.status, resp.headers, "catalog")
            if resp.status != 200:
                LOGGER.warning("Catalogue HTTP %s pour %s", resp.status, query)
                return []
            data = await resp.json()
            return data.get("items", [])
    except Exception as e:
        LOGGER.error("Erreur catalogue %s: %s", query, e)
        return []

async def detail_item(item_id, base_url, limiter, session, headers):
    url = f"{base_url}/api/v2/items/{item_id}"
    await limiter.wait("detail")
    try:
        async with session.get(url, headers=headers, timeout=8) as resp:
            await limiter.register_response(resp.status, resp.headers, "detail")
            if resp.status != 200:
                return None
            data = await resp.json()
            return data.get("item", {})
    except Exception:
        return None

# ---------- Scan principal ----------
async def scan_search(search, cfg, blacklist, seen_ids, seen_meta,
                      limiter, session, base_url, headers):
    query = search["query"]
    name = search.get("name", query)
    LOGGER.info(f"\n[API-TEST] {name} → {query}")

    items = await catalog_items(query, search.get("price_to"), base_url, limiter, session, headers)
    if not items:
        return []

    max_items = int(search.get("max_items", cfg.get("max_items_per_search", 15)))
    alerts = []
    price_history = load_json(PRICE_HISTORY_PATH, {})

    for item in items[:max_items]:
        item_id = str(item.get("id"))
        title = item.get("title", "")
        price = _positive_price(item.get("price"))
        if price is None:
            continue

        # 1. Vérifier âge
        created = item.get("created_at_ts") or item.get("created_at")
        fresh, age, reason = freshness_check(created, cfg)
        if not fresh:
            LOGGER.debug("  X Âge | %s", reason)
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        # 2. Vérifier vu (sauf si baisse de prix, simplifié ici)
        if item_already_seen(seen_ids, search, item_id):
            continue

        # 3. Vendeur pro
        seller = item.get("user", {})
        if cfg.get("exclude_professional_sellers", True) and (seller.get("is_business") or seller.get("is_pro")):
            LOGGER.debug("  X Vendeur Pro | %s", title[:60])
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        # 4. Filtres rapides
        blocked, _, _, _ = blacklist_check(title, title, blacklist)
        if blocked:
            LOGGER.debug("  X Blacklist | %s", title[:60])
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        category = search.get("category", "")
        sane, _ = category_sanity_check(category, title)
        if not sane:
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        empty, _ = empty_packaging_check(category, title, title)
        if empty:
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        # 5. Trouver la règle
        matched_rule = None
        for rule in search.get("rules", []):
            if rule_match(rule, title, title):
                matched_rule = rule
                break
        if matched_rule is None:
            continue

        # 6. Scoring avec catalogue
        total, resale_low, resale_high, margin_low, margin_high, roi_low = score_candidate(matched_rule, price, cfg)
        if margin_low is None:
            continue
        ref_price = float(matched_rule.get("market_avg", resale_low))
        min_margin = matched_rule.get("min_margin", cfg.get("min_margin", 25))
        if margin_low < float(min_margin):
            continue
        min_roi = matched_rule.get("min_roi_pct", cfg.get("min_roi_pct", 20))
        if roi_low < float(min_roi):
            continue

        # 7. Appel détail (pour les favoris et la description)
        detail = await detail_item(item_id, base_url, limiter, session, headers)
        if detail is None or detail.get("is_closed") or detail.get("status") in ("sold", "reserved"):
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        fav_count = detail.get("favourite_count")
        description = detail.get("description", "")
        real_price = _positive_price(detail.get("price"))
        if real_price is not None and real_price > 0:
            price = real_price
            total, resale_low, resale_high, margin_low, margin_high, roi_low = score_candidate(matched_rule, price, cfg)
            if margin_low is None or margin_low < float(min_margin) or roi_low < float(min_roi):
                continue

        # Re-vérifier les règles avec la description
        if not rule_match(matched_rule, title, description, deep=True):
            continue

        # 8. Score final
        motivation_hits = [w for w in cfg.get("seller_motivation_words", []) if term_present(f"{title} {description}", w)]
        score = opportunity_score(price, ref_price, margin_low, motivation_hits,
                                  age_hours=age, favourite_count=fav_count, cfg=cfg)

        # 9. Alerte
        size = "?"
        published_dt = parse_vinted_timestamp(detail.get("created_at_ts"))
        published_at = published_dt.isoformat(timespec="seconds") if published_dt else ""

        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "category": category,
            "search": search.get("name", "") + " / " + matched_rule.get("label", ""),
            "brand": matched_rule.get("brand", ""),
            "model": matched_rule.get("model", ""),
            "size": size,
            "opportunity_score": score,
            "title": title,
            "published_at": published_at,
            "age_minutes": int(round(age * 60)) if age is not None else "",
            "favourite_count": fav_count if fav_count is not None else "",
            "seller_type": "pro" if seller.get("is_business") else "particulier",
            "image_url": item.get("photo", {}).get("url", ""),
            "listing_price": round(price, 2),
            "total_buy_est": total,
            "resale_low": resale_low,
            "resale_high": resale_high,
            "margin_low": margin_low,
            "margin_high": margin_high,
            "roi_low": roi_low,
            "demand_score": matched_rule.get("demand_score", 5),
            "risk": "",
            "reason": f"prix ~ {price/ref_price*100:.0f}% de la référence",
            "url": f"{base_url}/items/{item_id}",
            "item_id": item_id,
        }

        alerts.append(row)
        LOGGER.info(f"  ★ SCORE {score}/10 | {freshness_label(age, cfg)} | {title[:58]} | {price:.2f}€ | marge +{margin_low:.2f}€")
        LOGGER.info(f"    {row['url']}")

        # Marquer comme vu
        mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
        mark_seen(seen_ids, alert_seen_key(item_id), seen_meta)

    return alerts

# ---------- Main ----------
async def main_async():
    cfg = load_json(CONFIG_PATH, {})
    if not cfg:
        LOGGER.error("config.json introuvable")
        return

    blacklist = load_json(BLACKLIST_PATH, {})

    # Charger état
    seen_ids = set()
    seen_meta = {}
    if SEEN_PATH.exists():
        raw = load_json(SEEN_PATH, [])
        if isinstance(raw, list):
            seen_ids = {str(x) for x in raw}
    if SEEN_META_PATH.exists():
        meta = load_json(SEEN_META_PATH, {})
        if isinstance(meta, dict):
            seen_meta = meta

    prune_seen_state(seen_ids, seen_meta, cfg.get("seen_retention_days", 30))

    limiter = AsyncRateLimiter(
        cfg.get("request_delay_min_seconds", 1.0),
        cfg.get("request_delay_max_seconds", 3.0),
        cfg.get("backoff_max_seconds", 60.0),
    )

    base_url = cfg.get("base_url", "https://www.vinted.be").rstrip("/")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }

    async with aiohttp.ClientSession() as session:
        searches = cfg.get("searches", [])
        tasks = [scan_search(s, cfg, blacklist, seen_ids, seen_meta, limiter, session, base_url, headers) for s in searches]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        total_alerts = 0
        for res in results:
            if isinstance(res, Exception):
                LOGGER.error("Erreur recherche: %s", res)
            else:
                total_alerts += len(res)

        save_seen_state(seen_ids, seen_meta)
        LOGGER.info(f"\n[API-TEST] Terminé : {total_alerts} nouvelles alertes trouvées.")

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        LOGGER.info("Arrêt demandé")

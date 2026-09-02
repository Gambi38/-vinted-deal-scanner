#!/usr/bin/env python3
# VINTED_API_AIOHTTP_V2
# Scanner autonome : catalogue, détails et notifications utilisent aiohttp.

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
FILTRES_PATH = ROOT / "filtres.json"
SEEN_PATH = DATA_DIR / "annonces_vues.json"
SEEN_META_PATH = DATA_DIR / "annonces_vues_meta.json"
PRICE_HISTORY_PATH = DATA_DIR / "annonces_prix.json"
ALERTS_CSV = DATA_DIR / "alertes.csv"

ALERT_FIELDS = [
    "timestamp", "category", "search", "brand", "model", "size",
    "opportunity_score", "title", "published_at", "age_minutes",
    "view_count", "favourite_count", "seller_type", "previous_price",
    "price_drop_pct", "image_url", "listing_price", "total_buy_est",
    "resale_low", "resale_high", "margin_low", "margin_high", "roi_low",
    "demand_score", "risk", "reason", "url", "item_id",
]

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
    max_age = float(cfg.get("max_listing_age_hours", 3))
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

def convert_personal_filter(entry):
    if not isinstance(entry, dict) or not entry.get("actif", True):
        return []
    name = str(entry.get("nom", "")).strip()
    category = str(entry.get("categorie", "")).strip()
    queries = entry.get("recherches_vinted", [])
    if isinstance(queries, str):
        queries = [queries]
    single = str(entry.get("recherche_vinted", "")).strip()
    if single:
        queries = [single, *queries]
    queries = list(dict.fromkeys(str(x).strip() for x in queries if str(x).strip()))
    resale_low = entry.get("revente_prudente")
    if not name or not category or not queries or resale_low is None:
        return []
    resale_high = entry.get("revente_haute", resale_low)
    rule = {
        "label": name,
        "brand": str(entry.get("marque", "")).strip(),
        "model": str(entry.get("modele", name)).strip(),
        "must_contain": list(entry.get("mots_obligatoires", [])),
        "any_contain": list(entry.get("un_des_mots", [])),
        "platform_any": list(entry.get("mots_plateforme", [])),
        "hardware_any": list(entry.get("indices_materiel", [])),
        "exclude": list(entry.get("mots_exclus", [])),
        "resale_low": float(resale_low),
        "resale_high": float(resale_high),
        "min_margin": float(entry.get("marge_minimum", 10)),
        "min_roi_pct": float(entry.get("roi_minimum", 30)),
        "demand_score": int(entry.get("score_demande", 5)),
    }
    if entry.get("materiel_dans_titre"):
        rule["hardware_in_title"] = True
    return [{
        "name": f"FILTRE - {name} / {query}",
        "category": category,
        "query": query,
        "price_to": float(entry.get("prix_recherche_max", float(resale_low) * 0.5)),
        "max_items": int(entry.get("nombre_annonces_a_lire", 35)),
        "rules": [rule],
    } for query in queries]

def apply_personal_filters(cfg, blacklist):
    data = load_json(FILTRES_PATH, {})
    if not isinstance(data, dict):
        return 0
    for word in data.get("mots_a_exclure", []):
        if word and word not in blacklist.setdefault("accessory_blacklist", []):
            blacklist["accessory_blacklist"].append(word)
    for word in data.get("mots_a_exclure_du_titre", []):
        if word and word not in blacklist.setdefault("title_accessory_blacklist", []):
            blacklist["title_accessory_blacklist"].append(word)
    searches = []
    for entry in data.get("articles_a_surveille", []):
        searches.extend(convert_personal_filter(entry))
    cfg["searches"] = searches + list(cfg.get("searches", []))
    return len(searches)

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

def _optional_count(value):
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None

def opportunity_score(price, ref_price, margin_low, motivation_hits, age_hours=None,
                      favourite_count=None, view_count=None, cfg=None):
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
    cfg = cfg or {}
    favs = _optional_count(favourite_count)
    views = _optional_count(view_count)
    penalty = 0.0
    if favs is not None:
        penalty += favs * float(cfg.get("favourite_penalty_per_user", 0.25))
    if views is not None:
        penalty += views * float(cfg.get("view_penalty_per_view", 0.05))
    score -= min(penalty, float(cfg.get("popularity_penalty_cap", 2.0)))
    if (age_hours is not None and age_hours <= 5/60 and
            (favs is not None or views is not None) and
            (favs is None or favs == 0) and (views is None or views < 5)):
        score += float(cfg.get("hidden_deal_bonus", 1.0))
    return max(1, min(10, int(round(score))))

def reason_text(price, reference_price, motivation_hits, age_hours=None,
                favourite_count=None, view_count=None, cfg=None):
    parts = []
    if age_hours is not None:
        parts.append(freshness_label(age_hours, cfg or {}))
    if reference_price:
        parts.append(f"prix à environ {price/reference_price*100:.0f}% de la référence prudente")
    views = _optional_count(view_count)
    favs = _optional_count(favourite_count)
    if views is not None:
        parts.append(f"{views} vue{'s' if views > 1 else ''}")
    if favs is not None:
        parts.append(f"{favs} favori{'s' if favs > 1 else ''}")
    if motivation_hits:
        parts.append("vendeur motivé: " + ", ".join(motivation_hits[:2]))
    return "; ".join(parts) or "rapport achat/revente intéressant"

def ensure_alert_csv_schema():
    ALERTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not ALERTS_CSV.exists() or ALERTS_CSV.stat().st_size == 0:
        return list(ALERT_FIELDS)
    with ALERTS_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        previous = [x for x in (reader.fieldnames or []) if x]
        rows = list(reader)
    fields = list(dict.fromkeys(ALERT_FIELDS + previous))
    if fields != previous:
        tmp = ALERTS_CSV.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows({k: row.get(k, "") for k in fields} for row in rows)
        tmp.replace(ALERTS_CSV)
    return fields

def append_alert(row):
    fields = ensure_alert_csv_schema()
    new = not ALERTS_CSV.exists() or ALERTS_CSV.stat().st_size == 0
    with ALERTS_CSV.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if new:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})

async def ntfy_send(row, session):
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        LOGGER.warning("NTFY_TOPIC absent: alerte enregistrée mais non envoyée")
        return False
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{urllib.parse.quote(topic, safe='')}"
    body = (f"[{row['opportunity_score']}/10] {row['title']} | "
            f"Achat {row['listing_price']:.2f} EUR | revente "
            f"{row['resale_low']:.0f}-{row['resale_high']:.0f} EUR | "
            f"bénéfice {row['margin_low']:.2f} EUR | {row['reason']}")
    headers = {
        "Title": f"Vinted Deal {row['opportunity_score']}/10",
        "Priority": "high" if row["opportunity_score"] >= 8 else "default",
        "Tags": "moneybag,shopping_cart", "Click": row["url"],
        "Actions": f"view, Ouvrir Vinted, {row['url']}",
    }
    if row.get("image_url"):
        headers["Attach"] = row["image_url"]
    try:
        async with session.post(url, data=body.encode("utf-8"), headers=headers,
                                timeout=aiohttp.ClientTimeout(total=8)) as response:
            if 200 <= response.status < 300:
                return True
            LOGGER.error("ntfy HTTP %s", response.status)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        LOGGER.error("ntfy indisponible: %s", exc)
    return False

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
async def catalog_items(query, price_to, base_url, limiter, session, headers, stats=None):
    url = f"{base_url}/api/v2/catalog/items"
    params = {"search_text": query, "order": "newest_first", "per_page": 40}
    if price_to is not None:
        params["price_to"] = float(price_to)
    await limiter.wait("catalog")
    if stats is not None:
        stats["catalog_requested"] += 1
    try:
        async with session.get(url, params=params, headers=headers, timeout=10) as resp:
            await limiter.register_response(resp.status, resp.headers, "catalog")
            if resp.status != 200:
                LOGGER.warning("Catalogue HTTP %s pour %s", resp.status, query)
                return []
            data = await resp.json()
            items = data.get("items", [])
            if stats is not None:
                stats["catalog_success"] += 1
                stats["catalog_items"] += len(items)
            return items
    except Exception as e:
        LOGGER.error("Erreur catalogue %s: %s", query, e)
        return []

async def detail_item(item_id, base_url, limiter, session, headers, stats=None):
    url = f"{base_url}/api/v2/items/{item_id}"
    await limiter.wait("detail")
    if stats is not None:
        stats["detail_requested"] += 1
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
                      limiter, session, base_url, headers, stats):
    query = search["query"]
    name = search.get("name", query)
    LOGGER.info(f"\n[API-TEST] {name} → {query}")

    items = await catalog_items(query, search.get("price_to"), base_url, limiter, session, headers, stats)
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
        # L'API catalogue omet souvent la date. Dans ce cas, on diffère la
        # décision jusqu'au détail au lieu de rejeter silencieusement l'annonce.
        age = listing_age_hours(created)
        if age is not None and age > float(cfg.get("max_listing_age_hours", 3)):
            LOGGER.debug("  X Âge | %.1fh", age)
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
        detail = await detail_item(item_id, base_url, limiter, session, headers, stats)
        if detail is None or detail.get("is_closed") or detail.get("status") in ("sold", "reserved"):
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        fav_count = detail.get("favourite_count")
        view_count = detail.get("view_count")
        description = detail.get("description", "")

        detail_created = (detail.get("created_at_ts") or detail.get("created_at")
                          or detail.get("uploaded_at"))
        if detail_created is not None:
            fresh, age, age_reason = freshness_check(detail_created, cfg)
            if not fresh:
                mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
                continue
            stats["age_known"] += 1
        elif age is None:
            stats["age_unknown"] += 1
            # Une date absente ne doit pas devenir un rejet permanent : elle
            # pourra être réessayée au prochain scan (maximum contrôlé ailleurs).
            LOGGER.warning("  ? Âge absent dans catalogue et détail | %s", item_id)
            continue
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
        score = opportunity_score(
            price, ref_price, margin_low, motivation_hits,
            age_hours=age, favourite_count=fav_count,
            view_count=view_count, cfg=cfg,
        )

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
            "view_count": view_count if view_count is not None else "",
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
            "reason": reason_text(
                price, ref_price, motivation_hits, age_hours=age,
                cfg=cfg, favourite_count=fav_count,
                view_count=view_count,
            ),
            "url": f"{base_url}/items/{item_id}",
            "item_id": item_id,
        }

        alerts.append(row)
        append_alert(row)
        if await ntfy_send(row, session):
            stats["notifications_sent"] += 1
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
    personal_count = apply_personal_filters(cfg, blacklist)
    LOGGER.info("%s recherches personnelles ajoutées", personal_count)

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

    stats = {key: 0 for key in (
        "catalog_requested", "catalog_success", "catalog_items",
        "detail_requested", "age_known", "age_unknown",
        "notifications_sent",
    )}

    async with aiohttp.ClientSession() as session:
        # Crée les cookies de session avant l'API. Un échec ici est informatif,
        # mais les appels catalogue décideront de l'état de santé réel.
        try:
            async with session.get(base_url + "/", headers={
                **headers, "Accept": "text/html,application/xhtml+xml"
            }, timeout=12) as resp:
                LOGGER.info("Initialisation session HTTP %s", resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            LOGGER.warning("Initialisation session impossible: %s", exc)

        searches = cfg.get("searches", [])
        semaphore = asyncio.Semaphore(int(cfg.get("api_max_concurrency", 3)))

        async def bounded_scan(search):
            async with semaphore:
                return await scan_search(
                    search, cfg, blacklist, seen_ids, seen_meta,
                    limiter, session, base_url, headers, stats,
                )

        tasks = [bounded_scan(s) for s in searches]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        total_alerts = 0
        for res in results:
            if isinstance(res, Exception):
                LOGGER.error("Erreur recherche: %s", res)
            else:
                total_alerts += len(res)

        save_seen_state(seen_ids, seen_meta)
        LOGGER.info("Santé API | catalogues %s/%s | articles %s | détails %s | "
                    "âges connus %s | inconnus %s | notifications %s",
                    stats["catalog_success"], stats["catalog_requested"],
                    stats["catalog_items"], stats["detail_requested"],
                    stats["age_known"], stats["age_unknown"],
                    stats["notifications_sent"])
        LOGGER.info(f"\n[API] Terminé : {total_alerts} nouvelles alertes trouvées.")

        requested = stats["catalog_requested"]
        if requested and stats["catalog_success"] / requested < 0.5:
            raise RuntimeError("Moins de 50% des catalogues ont répondu: scan invalide")
        if requested and stats["catalog_success"] and stats["catalog_items"] == 0:
            raise RuntimeError("Catalogues vides: scan probablement bloqué par Vinted")

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        LOGGER.info("Arrêt demandé")

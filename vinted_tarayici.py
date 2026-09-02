#!/usr/bin/env python3
# VINTED_TARAYICI_V7_5_API_ONLY
"""
Scanneur Vinted full‑API avec :
- catalogue JSON (order=newest_first)
- appels détail HTTP uniquement pour les cibles rentables
- parallélisation des recherches (asyncio.gather)
- backoff 429 et jitter
- scoring, favoris, baisses de prix
- exclusion vendeurs Pro, filtres, blacklist, exemples
"""
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
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------- Configuration initiale ----------
ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("VINTED_DATA_DIR", str(ROOT / "runtime_data"))).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = ROOT / "config.json"
BLACKLIST_PATH = ROOT / "blacklist.json"
FILTRES_PATH = ROOT / "filtres.json"
EXEMPLES_PATH = ROOT / "exemples.txt"

SEEN_PATH = DATA_DIR / "annonces_vues.json"
SEEN_META_PATH = DATA_DIR / "annonces_vues_meta.json"
PRICE_HISTORY_PATH = DATA_DIR / "annonces_prix.json"
ALERTS_CSV = DATA_DIR / "alertes.csv"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
LOGGER = logging.getLogger("vinted_tarayici")

# ---------- Utilitaires JSON / CSV / Env ----------
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

def charger_json_avec_ancien_nom(nouveau, ancien, default):
    if nouveau.exists():
        return load_json(nouveau, default)
    if ancien and ancien.exists():
        return load_json(ancien, default)
    return default

def env_bool(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

def apply_env_overrides(cfg):
    """Applique les variables d'environnement sur les paramètres scalaires."""
    mapping = {
        "VINTED_BASE_URL": ("base_url", str),
        "VINTED_MAX_LISTING_AGE_HOURS": ("max_listing_age_hours", float),
        "VINTED_REQUEST_DELAY_MIN_SECONDS": ("request_delay_min_seconds", float),
        "VINTED_REQUEST_DELAY_MAX_SECONDS": ("request_delay_max_seconds", float),
        "VINTED_BACKOFF_MAX_SECONDS": ("backoff_max_seconds", float),
        "VINTED_STARTUP_JITTER_MAX_SECONDS": ("startup_jitter_max_seconds", float),
        "VINTED_RUN_BUDGET_SECONDS": ("run_budget_seconds", float),
        "VINTED_MAX_ITEMS_PER_SEARCH": ("max_items_per_search", int),
        "VINTED_PRICE_DROP_ALERT_PCT": ("price_drop_alert_pct", float),
        "VINTED_SEEN_RETENTION_DAYS": ("seen_retention_days", int),
        "VINTED_UNKNOWN_AGE_MAX_ATTEMPTS": ("unknown_age_max_attempts", int),
    }
    for env_name, (config_name, caster) in mapping.items():
        raw = os.getenv(env_name)
        if raw is not None and raw.strip() != "":
            try:
                cfg[config_name] = caster(raw.strip())
            except ValueError:
                raise ValueError(f"{env_name} invalide")
    cfg["reject_unknown_listing_age"] = env_bool("VINTED_REJECT_UNKNOWN_LISTING_AGE",
                                                 cfg.get("reject_unknown_listing_age", True))
    cfg["exclude_professional_sellers"] = env_bool("VINTED_EXCLUDE_PRO_SELLERS",
                                                   cfg.get("exclude_professional_sellers", True))
    return cfg

def validate_runtime_config(cfg):
    """Vérifications de cohérence."""
    if float(cfg.get("request_delay_min_seconds", 1.0)) < 0.5:
        raise ValueError("request_delay_min_seconds doit être >= 0.5")
    if float(cfg.get("request_delay_max_seconds", 3.0)) < float(cfg.get("request_delay_min_seconds", 1.0)):
        raise ValueError("request_delay_max_seconds doit être >= request_delay_min_seconds")
    if float(cfg.get("backoff_max_seconds", 60)) < float(cfg.get("request_delay_max_seconds", 3.0)):
        raise ValueError("backoff_max_seconds doit être >= request_delay_max_seconds")
    if float(cfg.get("run_budget_seconds", 480)) <= 0:
        raise ValueError("run_budget_seconds > 0")
    if float(cfg.get("max_listing_age_hours", 24)) <= 0:
        raise ValueError("max_listing_age_hours > 0")
    if int(cfg.get("max_items_per_search", 15)) <= 0:
        raise ValueError("max_items_per_search > 0")
    base_url = str(cfg.get("base_url", "")).strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise ValueError("base_url doit être en HTTPS")
    cfg["base_url"] = base_url

# ---------- Rate Limiter asynchrone ----------
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
                LOGGER.debug("Rate limit %s: wait %.2f s", kind, remaining)
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

# ---------- Normalisation et parsing ----------
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

def parse_price(text):
    prices = re.findall(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*€", text or "")
    if not prices:
        return None
    try:
        return min(float(p.replace(",", ".")) for p in prices)
    except ValueError:
        return None

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
    # Fallback: on tente de parser le texte "Ajouté il y a X min" si value est une chaîne
    if isinstance(value, str) and "ajouté" in value.lower():
        match = re.search(r"(\d+)\s*(min|h|heure|jour|jours)", value.lower())
        if match:
            num = int(match.group(1))
            unit = match.group(2)
            if unit.startswith("min"):
                return num / 60.0
            elif unit.startswith("h") or unit.startswith("heur"):
                return float(num)
            elif unit.startswith("jour"):
                return float(num * 24)
    return None

def freshness_check(value, cfg, now=None):
    max_age = float(cfg.get("max_listing_age_hours", 24))
    age = listing_age_hours(value, now)
    if age is None:
        if cfg.get("reject_unknown_listing_age", True):
            return False, None, "âge inconnu"
        return True, None, "âge inconnu toléré"
    # On rejette si dépasse max_age
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

# ---------- Gestion des annonces vues ----------
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

def forget_seen(seen_ids, key, seen_meta=None):
    key = str(key)
    seen_ids.discard(key)
    if seen_meta is not None:
        seen_meta.pop(key, None)

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
            # On attribue une date pour les anciennes entrées
            if seen_meta is not None:
                seen_meta[str(key)] = current
            continue
        try:
            t = float(ts)
        except (TypeError, ValueError):
            t = current
        if t < cutoff:
            forget_seen(seen_ids, key, seen_meta)
            removed += 1
    if seen_meta:
        for orphan in set(seen_meta) - {str(k) for k in seen_ids}:
            seen_meta.pop(orphan, None)
    return removed

def save_seen_state(seen_ids, seen_meta):
    keys = sorted(str(k) for k in seen_ids)
    save_json(SEEN_PATH, keys)
    meta = {k: float(seen_meta.get(k, time.time())) for k in keys}
    save_json(SEEN_META_PATH, meta)

# ---------- Gestion des prix et baisses ----------
def price_drop_event(price_history, item_id, current_price, threshold_pct=20):
    if not price_history:
        return None
    current = _positive_price(current_price)
    if current is None:
        return None
    entry = price_history.get(str(item_id))
    if not entry:
        return None
    baseline = _positive_price(entry.get("baseline_price") or entry.get("price"))
    if baseline is None or current >= baseline:
        return None
    drop = (baseline - current) / baseline * 100
    if drop + 1e-9 < float(threshold_pct):
        return None
    return {
        "previous_price": round(baseline, 2),
        "current_price": round(current, 2),
        "price_drop_pct": round(drop, 1),
    }

def remember_price(price_history, item_id, current_price, now=None, reset=False):
    if not price_history:
        return
    current = _positive_price(current_price)
    if current is None:
        return
    key = str(item_id)
    old = price_history.get(key, {})
    baseline = _positive_price(old.get("baseline_price") or old.get("price"))
    if reset or baseline is None:
        baseline = current
    else:
        baseline = max(baseline, current)
    price_history[key] = {
        "baseline_price": round(baseline, 2),
        "last_price": round(current, 2),
        "seen_at": float(now if now is not None else time.time()),
    }

def prune_price_history(price_history, retention_days=30, now=None):
    if not price_history:
        return 0
    current = float(now if now is not None else time.time())
    cutoff = current - float(retention_days) * 86400
    removed = 0
    for item_id, entry in list(price_history.items()):
        if not isinstance(entry, dict):
            price_history.pop(item_id, None)
            removed += 1
            continue
        seen_at = entry.get("seen_at")
        try:
            t = float(seen_at) if seen_at is not None else current
        except (TypeError, ValueError):
            t = current
        if t < cutoff:
            price_history.pop(item_id, None)
            removed += 1
    return removed

# ---------- Blacklist et filtres ----------
def blacklist_check(title, text, blacklist):
    combined = norm(f"{title} {text}")
    # Titre
    title_hits = [w for w in blacklist.get("title_accessory_blacklist", []) if term_present(title, w)]
    if title_hits:
        return True, "title_accessory_blacklist", title_hits[:3], []
    # Groupes
    for group in ("hard_blacklist", "fake_blacklist", "accessory_blacklist"):
        hits = [w for w in blacklist.get(group, []) if term_present(combined, w)]
        if hits:
            return True, group, hits[:3], []
    risks = [w for w in blacklist.get("suspicious_words", []) if term_present(combined, w)]
    return False, "", [], risks[:3]

def low_value_game_check(title, text, blacklist):
    combined = norm(f"{title} {text}")
    low_games = [norm(w) for w in blacklist.get("low_value_games", [])]
    collector = [norm(w) for w in blacklist.get("collector_exception_words", [])]
    hits = [w for w in low_games if term_present(combined, w)]
    if not hits:
        return False, []
    if any(term_present(combined, w) for w in collector):
        return False, []
    return True, hits[:3]

def category_sanity_check(category, title):
    # Implémentation simplifiée, copiée de l'original
    if category.startswith("JEU_"):
        merch = ["steelbook", "pin", "badge", "figurine", "amiibo", "poster", "artbook", "guide", "boite vide", "empty box"]
        hits = [w for w in merch if term_present(title, w)]
        if hits:
            return False, "objet dérivé/accessoire: " + ", ".join(hits[:3])
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
    phrases = ["boite vide", "boîte vide", "boitier vide", "sans jeu", "empty box", "box only", "case only"]
    combined = norm(f"{title} {text}")
    hits = [p for p in phrases if term_present(combined, p)]
    if hits:
        return True, hits[:3]
    return False, []

def ignored_brand_check(text, cfg):
    hits = [w for w in cfg.get("ignored_brands", []) if term_present(text, w)]
    return hits[:3]

def condition_check(text, cfg, rule):
    bad = [w for w in cfg.get("fatal_condition_words", []) if term_present(text, w)]
    if not bad:
        return True, [], []
    if rule.get("rare_collectible"):
        rare = [w for w in cfg.get("rare_exception_words", []) if term_present(text, w)]
        if len(rare) >= cfg.get("rare_exception_min_hits", 2):
            return True, bad[:3], rare[:3]
    return False, bad[:3], []

def electronics_condition_check(title, text, cfg, category):
    if category not in {"CONSOLE", "ELECTRONIQUE"}:
        return True, ""
    combined = f"{title} {text}"
    fatal = [w for w in cfg.get("fatal_electronics_condition_words", []) if term_present(combined, w)]
    if fatal:
        return False, "état: " + ", ".join(fatal[:3])
    power = [w for w in cfg.get("power_missing_words", []) if term_present(combined, w)]
    if power:
        return False, "alimentation manquante: " + ", ".join(power[:2])
    return True, ""

# ---------- Règles et scoring ----------
def rule_match(rule, title, text, deep=False):
    title_n = norm(title)
    full = norm(f"{title} {text}")
    must = rule.get("must_contain", [])
    any_kw = rule.get("any_contain", [])
    exclude = rule.get("exclude", [])
    tolerant = rule.get("tolerer_fautes", False)

    if must and not all(term_present(title_n, w) if not tolerant else _souple(title_n, w) for w in must):
        return False
    if any_kw and not any(term_present(title_n, w) if not tolerant else _souple(title_n, w) for w in any_kw):
        return False
    if exclude and any(term_present(full, w) for w in exclude):
        return False
    if not deep:
        return True
    # Vérifications supplémentaires pour deep
    platform = rule.get("platform_any", [])
    hardware = rule.get("hardware_any", [])
    exact_titles = rule.get("exact_title_any", [])
    if platform and not any(term_present(full, w) for w in platform):
        return False
    if hardware:
        hardware_text = title_n if rule.get("hardware_in_title") else full
        if not any(term_present(hardware_text, w) for w in hardware):
            return False
    if exact_titles and not any(norm(title) == norm(w) for w in exact_titles):
        return False
    return True

def _souple(text, term):
    # Tolérance légère aux fautes
    return term_present(text, term)  # simplifié ici, on peut réimplémenter la distance de Levenshtein si besoin

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

def fee_estimate(price, cfg):
    bp = cfg.get("buyer_protection_estimate", {})
    return (float(bp.get("fixed", 0.70)) +
            float(bp.get("pct", 0.05)) * price +
            float(cfg.get("shipping_estimate", 4.50)))

def opportunity_score(price, ref_price, margin_low, motivation_hits, authenticity_risk=False,
                      rare_condition=False, age_hours=None, favourite_count=None, cfg=None):
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
    if authenticity_risk:
        score -= 1
    if rare_condition:
        score -= 1
    return max(1, min(10, int(round(score))))

def reason_text(price, ref_price, motivation_hits, authenticity_risk=False,
                rare_condition_hits=None, age_hours=None, cfg=None, favourite_count=None, price_drop=None):
    parts = []
    if price_drop:
        parts.append(f"prix baissé {price_drop['previous_price']:.2f}→{price_drop['current_price']:.2f}€ (-{price_drop['price_drop_pct']:.0f}%)")
    if age_hours is not None:
        parts.append(freshness_label(age_hours, cfg or {}))
    if ref_price:
        parts.append(f"prix ~ {price/ref_price*100:.0f}% de la référence")
    if favourite_count is not None:
        favs = int(favourite_count) if favourite_count is not None else 0
        if favs == 0 and age_hours is not None and age_hours <= 5/60:
            parts.append("0 favori: affaire discrète")
        elif favs > 0:
            parts.append(f"{favs} favori{'s' if favs>1 else ''}")
    if motivation_hits:
        parts.append("vendeur motivé: " + ", ".join(motivation_hits[:2]))
    if authenticity_risk:
        parts.append("authenticité à vérifier")
    if rare_condition_hits:
        parts.append("état atypique toléré (pièce rare)")
    return "; ".join(parts) if parts else "bon rapport achat/revente"

def extract_size(text):
    # simplifié
    m = re.search(r"W\s?(\d{2})\s*[xX/ -]?\s*L\s?(\d{2})", text or "", re.I)
    if m:
        return f"W{m.group(1)} L{m.group(2)}"
    m = re.search(r"(3[4-9]|4[0-9]|5[0-2])", text or "")
    if m:
        return m.group(1)
    return "?"

# ---------- Appels API ----------
async def catalog_items(query, price_to, base_url, limiter, session, headers):
    """Récupère le catalogue JSON (1ère page) pour une recherche."""
    url = f"{base_url}/api/v2/catalog/items"
    params = {
        "search_text": query,
        "order": "newest_first",
        "per_page": 40,  # on prend beaucoup mais on ne garde que les récentes
    }
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
            items = data.get("items", [])
            return items
    except Exception as e:
        LOGGER.error("Erreur catalogue %s: %s", query, e)
        return []

async def detail_item(item_id, base_url, limiter, session, headers):
    """Récupère les détails d'une annonce."""
    url = f"{base_url}/api/v2/items/{item_id}"
    await limiter.wait("detail")
    try:
        async with session.get(url, headers=headers, timeout=8) as resp:
            await limiter.register_response(resp.status, resp.headers, "detail")
            if resp.status != 200:
                LOGGER.debug("Détail HTTP %s pour %s", resp.status, item_id)
                return None
            data = await resp.json()
            return data.get("item", {})
    except Exception as e:
        LOGGER.debug("Erreur détail %s: %s", item_id, e)
        return None

# ---------- Fonction principale de scan d'une recherche ----------
async def scan_search(search, cfg, blacklist, seen_ids, seen_meta, price_history,
                      limiter, session, base_url, headers):
    query = search["query"]
    name = search.get("name", query)
    LOGGER.info(f"\n[SCAN] {name} → {query}")

    items = await catalog_items(query, search.get("price_to"), base_url, limiter, session, headers)
    if not items:
        return []

    # Filtrer les items déjà vus et les vendeurs Pro
    max_items = int(search.get("max_items", cfg.get("max_items_per_search", 15)))
    candidates = []
    for item in items[:max_items]:
        item_id = str(item.get("id"))
        # Vérifier si déjà vu (sauf baisses de prix)
        price_drop = None
        current_price = _positive_price(item.get("price"))
        if current_price is not None:
            price_drop = price_drop_event(price_history, item_id, current_price,
                                          threshold_pct=cfg.get("price_drop_alert_pct", 20))
        if not price_drop and item_already_seen(seen_ids, search, item_id):
            continue

        # Vérifier l'âge (catalogue)
        created = item.get("created_at_ts") or item.get("created_at")
        fresh, age, reason = freshness_check(created, cfg)
        if not fresh:
            LOGGER.debug("  X %s | %s", reason, item.get("title", "")[:60])
            # On marque comme vu pour éviter de re-tester
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        # Vérifier vendeur Pro
        seller = item.get("user", {})
        is_pro = seller.get("is_business") or seller.get("is_pro")
        if cfg.get("exclude_professional_sellers", True) and is_pro:
            LOGGER.debug("  X Vendeur Pro | %s", item.get("title", "")[:60])
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        # Prix
        price = _positive_price(item.get("price"))
        if price is None:
            continue

        candidates.append({
            "item": item,
            "item_id": item_id,
            "title": item.get("title", ""),
            "price": price,
            "created_at_ts": created,
            "age_hours": age,
            "seller_is_business": is_pro,
            "favourite_count": item.get("favourite_count"),
            "image_url": item.get("photo", {}).get("url", ""),
            "url": f"{base_url}/items/{item_id}",
            "price_drop": price_drop,
        })

    # Appliquer les règles de recherche pour chaque candidat
    alerts = []
    for c in candidates:
        title = c["title"]
        text = title  # pas de description dans le catalogue, on utilisera le titre + détails plus tard
        price = c["price"]

        # Filtres rapides (blacklist, marques, etc.)
        blocked, group, hits, risks = blacklist_check(title, text, blacklist)
        if blocked:
            LOGGER.debug("  X Blacklist | %s", title[:60])
            mark_seen(seen_ids, search_seen_key(search, c["item_id"]), seen_meta)
            continue

        low_val, low_hits = low_value_game_check(title, text, blacklist)
        if low_val:
            LOGGER.debug("  X Jeu faible valeur | %s", title[:60])
            mark_seen(seen_ids, search_seen_key(search, c["item_id"]), seen_meta)
            continue

        category = search.get("category", "")
        sane, reason = category_sanity_check(category, title)
        if not sane:
            LOGGER.debug("  X Catégorie | %s", reason)
            mark_seen(seen_ids, search_seen_key(search, c["item_id"]), seen_meta)
            continue

        empty, _ = empty_packaging_check(category, title, text)
        if empty:
            LOGGER.debug("  X Boîte vide | %s", title[:60])
            mark_seen(seen_ids, search_seen_key(search, c["item_id"]), seen_meta)
            continue

        # Trouver la règle correspondante
        matched_rule = None
        for rule in search.get("rules", []):
            if rule_match(rule, title, text):
                matched_rule = rule
                break
        if matched_rule is None:
            continue

        # Scoring rapide avec les données catalogue
        total, resale_low, resale_high, margin_low, margin_high, roi_low = score_candidate(matched_rule, price, cfg)
        if margin_low is None:
            continue
        ref_price = float(matched_rule.get("market_avg", resale_low))
        max_buy_ratio = matched_rule.get("max_buy_ratio", cfg.get("max_buy_ratio_default", 0.40))
        if max_buy_ratio and ref_price > 0 and price > ref_price * float(max_buy_ratio):
            continue
        min_margin = matched_rule.get("min_margin", cfg.get("category_min_margin", {}).get(category, cfg.get("min_margin", 25)))
        if margin_low < float(min_margin):
            continue
        min_roi = matched_rule.get("min_roi_pct", cfg.get("min_roi_pct", 20))
        if roi_low < float(min_roi):
            continue

        # Maintenant, on va chercher les détails (favoris réels, description, âge exact, etc.)
        detail = await detail_item(c["item_id"], base_url, limiter, session, headers)
        if detail is None:
            # On ne marque pas comme vu, on réessaiera plus tard
            continue

        # Vérifier disponibilité
        if detail.get("is_closed") or detail.get("is_reserved") or detail.get("is_hidden") or detail.get("status") in ("sold", "reserved"):
            LOGGER.debug("  X Vendue/réservée | %s", title[:60])
            mark_seen(seen_ids, search_seen_key(search, c["item_id"]), seen_meta)
            continue

        # Récupérer les vrais favoris
        fav_count = detail.get("favourite_count") or c["favourite_count"]
        # Vérifier l'âge avec le détail (peut être plus précis)
        created_det = detail.get("created_at_ts") or c["created_at_ts"]
        fresh, age_det, reason_age = freshness_check(created_det, cfg)
        if not fresh:
            LOGGER.debug("  X Âge (détail) | %s", reason_age)
            mark_seen(seen_ids, search_seen_key(search, c["item_id"]), seen_meta)
            continue

        # Vérifier vendeur Pro (si non détecté avant)
        seller_det = detail.get("user", {})
        is_pro = seller_det.get("is_business") or seller_det.get("is_pro")
        if cfg.get("exclude_professional_sellers", True) and is_pro:
            LOGGER.debug("  X Vendeur Pro (détail) | %s", title[:60])
            mark_seen(seen_ids, search_seen_key(search, c["item_id"]), seen_meta)
            continue

        # Récupérer le prix réel (peut être différent)
        real_price = _positive_price(detail.get("price"))
        if real_price is not None and real_price > 0:
            price = real_price
            # Recalculer le score avec le nouveau prix
            total, resale_low, resale_high, margin_low, margin_high, roi_low = score_candidate(matched_rule, price, cfg)
            if margin_low is None:
                continue
            ref_price = float(matched_rule.get("market_avg", resale_low))
            if max_buy_ratio and ref_price > 0 and price > ref_price * float(max_buy_ratio):
                continue
            if margin_low < float(min_margin):
                continue
            if roi_low < float(min_roi):
                continue

        # Vérifications supplémentaires avec la description
        description = detail.get("description", "")
        full_text = f"{title} {description}"
        # Re-vérifier blacklist sur description
        blocked, group, hits, risks = blacklist_check(title, description, blacklist)
        if blocked:
            LOGGER.debug("  X Blacklist (détail) | %s", title[:60])
            mark_seen(seen_ids, search_seen_key(search, c["item_id"]), seen_meta)
            continue

        low_val, low_hits = low_value_game_check(title, description, blacklist)
        if low_val:
            LOGGER.debug("  X Jeu faible valeur (détail) | %s", title[:60])
            mark_seen(seen_ids, search_seen_key(search, c["item_id"]), seen_meta)
            continue

        condition_ok, bad_hits, rare_hits = condition_check(full_text, cfg, matched_rule)
        if not condition_ok:
            LOGGER.debug("  X État rédhibitoire | %s", ", ".join(bad_hits))
            mark_seen(seen_ids, search_seen_key(search, c["item_id"]), seen_meta)
            continue

        elect_ok, elect_reason = electronics_condition_check(title, description, cfg, category)
        if not elect_ok:
            LOGGER.debug("  X Électronique | %s", elect_reason)
            mark_seen(seen_ids, search_seen_key(search, c["item_id"]), seen_meta)
            continue

        # Vérification profonde des règles
        if not rule_match(matched_rule, title, description, deep=True):
            LOGGER.debug("  X Règle profonde non satisfaite | %s", title[:60])
            mark_seen(seen_ids, search_seen_key(search, c["item_id"]), seen_meta)
            continue

        # Motivation du vendeur
        motivation_hits = [w for w in cfg.get("seller_motivation_words", []) if term_present(full_text, w)]

        # Score final
        score = opportunity_score(
            price, ref_price, margin_low, motivation_hits,
            authenticity_risk=bool(matched_rule.get("authenticity_risk")),
            rare_condition=bool(rare_hits),
            age_hours=age_det,
            favourite_count=fav_count,
            cfg=cfg
        )

        reason = reason_text(
            price, ref_price, motivation_hits,
            authenticity_risk=bool(matched_rule.get("authenticity_risk")),
            rare_condition_hits=bad_hits if rare_hits else None,
            age_hours=age_det,
            cfg=cfg,
            favourite_count=fav_count,
            price_drop=c.get("price_drop")
        )

        # Prix suspect
        if matched_rule.get("suspicious_price_ratio"):
            threshold = float(matched_rule.get("suspicious_price_ratio"))
            if resale_low and price <= resale_low * threshold:
                reason += "; prix très bas, vérifier authenticité"
                score = min(score, 6)

        size = extract_size(full_text)
        published_dt = parse_vinted_timestamp(detail.get("created_at_ts"))
        published_at = published_dt.isoformat(timespec="seconds") if published_dt else ""

        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "category": category,
            "search": search.get("name", "") + " / " + matched_rule.get("label", ""),
            "brand": matched_rule.get("brand", search.get("name", "")),
            "model": matched_rule.get("model", matched_rule.get("label", "")),
            "size": size,
            "opportunity_score": score,
            "title": title,
            "published_at": published_at,
            "age_minutes": int(round(age_det * 60)) if age_det is not None else "",
            "favourite_count": fav_count if fav_count is not None else "",
            "seller_type": "pro" if is_pro else "particulier" if is_pro is False else "inconnu",
            "previous_price": c.get("price_drop", {}).get("previous_price", ""),
            "price_drop_pct": c.get("price_drop", {}).get("price_drop_pct", ""),
            "image_url": detail.get("photo", {}).get("url") or c.get("image_url", ""),
            "listing_price": round(price, 2),
            "total_buy_est": total,
            "resale_low": resale_low,
            "resale_high": resale_high,
            "margin_low": margin_low,
            "margin_high": margin_high,
            "roi_low": roi_low,
            "demand_score": matched_rule.get("demand_score", 5),
            "risk": ", ".join(dict.fromkeys(risks)),
            "reason": reason,
            "url": c["url"],
            "item_id": c["item_id"],
        }

        # Envoyer alerte
        alerts.append(row)
        append_alert(row)
        ntfy_send(row)

        LOGGER.info(f"  ★ SCORE {score}/10 | {freshness_label(age_det, cfg)} | {title[:58]} | {price:.2f}€ | marge +{margin_low:.2f}€")
        LOGGER.info(f"    {c['url']}")

        # Marquer comme vu pour cette recherche
        mark_seen(seen_ids, search_seen_key(search, c["item_id"]), seen_meta)
        # Marquer l'alerte globale
        mark_seen(seen_ids, alert_seen_key(c["item_id"]), seen_meta)

    # Mettre à jour l'historique des prix
    for c in candidates:
        remember_price(price_history, c["item_id"], c["price"], reset=c.get("price_drop") is not None)

    return alerts

# ---------- Alertes (CSV + NTFY) ----------
ALERT_FIELDS = [
    "timestamp", "category", "search", "brand", "model", "size", "opportunity_score",
    "title", "published_at", "age_minutes", "favourite_count", "seller_type",
    "previous_price", "price_drop_pct", "image_url", "listing_price", "total_buy_est",
    "resale_low", "resale_high", "margin_low", "margin_high", "roi_low",
    "demand_score", "risk", "reason", "url", "item_id"
]

def ensure_alert_csv_schema():
    ALERTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not ALERTS_CSV.exists() or ALERTS_CSV.stat().st_size == 0:
        return list(ALERT_FIELDS)
    with open(ALERTS_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        previous = reader.fieldnames or []
    fields = list(dict.fromkeys(ALERT_FIELDS + previous))
    if previous == fields:
        return fields
    tmp = ALERTS_CSV.with_suffix(ALERTS_CSV.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        # On réécrit les lignes existantes avec les colonnes manquantes
        with open(ALERTS_CSV, "r", encoding="utf-8-sig", newline="") as old:
            reader = csv.DictReader(old)
            for row in reader:
                writer.writerow({key: row.get(key, "") for key in fields})
    tmp.replace(ALERTS_CSV)
    return fields

def append_alert(row):
    fields = ensure_alert_csv_schema()
    new = not ALERTS_CSV.exists()
    with open(ALERTS_CSV, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})

def ntfy_send(row):
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{urllib.parse.quote(topic, safe='')}"
    title = f"Vinted Deal {row['opportunity_score']}/10"
    body = (
        f"[{row['opportunity_score']}/10] {row.get('category','')} {row.get('brand','')} {row.get('model','')} | "
        f"Achat {row['listing_price']:.2f}€ → Revente {row['resale_low']:.0f}-{row['resale_high']:.0f}€ | "
        f"Marge {row['margin_low']:.2f}€ | Favoris {row.get('favourite_count','?')} | {row['reason']} | {row['url']}"
    )
    headers = {
        "Title": title,
        "Priority": "high" if row['opportunity_score'] >= 8 else "default",
        "Tags": "moneybag,shopping_cart",
        "Click": row['url'],
        "Actions": f"view, Ouvrir Vinted, {row['url']}",
    }
    if row.get("image_url"):
        headers["Attach"] = row["image_url"]
    try:
        import urllib.request
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        LOGGER.error("ntfy error: %s", e)
        return False

# ---------- Main ----------
async def main():
    # Chargement config
    cfg = charger_json_avec_ancien_nom(CONFIG_PATH, ROOT / "configuration.json", {})
    if not cfg:
        raise FileNotFoundError("config.json introuvable")

    apply_env_overrides(cfg)
    validate_runtime_config(cfg)

    blacklist = charger_json_avec_ancien_nom(BLACKLIST_PATH, ROOT / "liste_noire.json", {})
    # Charger filtres personnalisés (filtres.json)
    filtres = load_json(FILTRES_PATH, {})
    # Appliquer les filtres personnels (similaire à l'original)
    # (on ne réimplémente pas toute la logique de conversion, on suppose que config.json contient déjà les recherches)

    # Charger exemples.txt
    exemples = []
    if EXEMPLES_PATH.exists():
        for line in EXEMPLES_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "/items/" in line:
                exemples.append(line)
        LOGGER.info(f"Chargé {len(exemples)} exemples depuis exemples.txt")

    # Initialiser l'état
    seen_ids = set()
    seen_meta = {}
    if SEEN_PATH.exists():
        try:
            raw = load_json(SEEN_PATH, [])
            if isinstance(raw, list):
                seen_ids = {str(x) for x in raw}
        except:
            pass
    if SEEN_META_PATH.exists():
        try:
            meta = load_json(SEEN_META_PATH, {})
            if isinstance(meta, dict):
                seen_meta = meta
        except:
            pass
    # Migration
    old_seen = ROOT / "seen.json"
    if old_seen.exists() and not seen_ids:
        try:
            raw = load_json(old_seen, [])
            if isinstance(raw, list):
                seen_ids = {str(x) for x in raw}
        except:
            pass

    # Historique prix
    price_history = load_json(PRICE_HISTORY_PATH, {})
    if not isinstance(price_history, dict):
        price_history = {}

    # Pruner
    prune_seen_state(seen_ids, seen_meta, cfg.get("seen_retention_days", 30))
    prune_price_history(price_history, cfg.get("seen_retention_days", 30))

    # Rate limiter
    limiter = AsyncRateLimiter(
        cfg.get("request_delay_min_seconds", 1.0),
        cfg.get("request_delay_max_seconds", 3.0),
        cfg.get("backoff_max_seconds", 60.0),
    )

    # Jitter de démarrage
    startup_jitter = random.uniform(0, float(cfg.get("startup_jitter_max_seconds", 20)))
    if startup_jitter:
        LOGGER.info("Décalage démarrage: %.1f s", startup_jitter)
        await asyncio.sleep(startup_jitter)

    base_url = cfg.get("base_url", "https://www.vinted.be").rstrip("/")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }

    one_shot = "--once" in sys.argv

    async with aiohttp.ClientSession() as session:
        while True:
            cycle_start = time.monotonic()
            cycle_alerts = 0
            searches = cfg.get("searches", [])

            # Gestion des exemples (si configurés)
            # On pourrait les ajouter dynamiquement, mais on suppose qu'ils sont déjà dans config.json

            # Paralléliser les recherches
            tasks = []
            for search in searches:
                tasks.append(
                    scan_search(search, cfg, blacklist, seen_ids, seen_meta, price_history,
                                limiter, session, base_url, headers)
                )
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for res in results:
                if isinstance(res, Exception):
                    LOGGER.error("Erreur lors d'une recherche: %s", res)
                else:
                    cycle_alerts += len(res)

            # Sauvegarde d'état
            save_seen_state(seen_ids, seen_meta)
            save_price_history(price_history)

            LOGGER.info(f"\n[Cycle] {cycle_alerts} alertes, {len(searches)} recherches terminées.")

            if one_shot:
                break

            # Attendre avant le prochain cycle
            poll = float(cfg.get("poll_seconds", 300))
            LOGGER.info(f"Prochain scan dans {poll} secondes.")
            await asyncio.sleep(poll)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOGGER.info("Arrêt demandé")
    except Exception as e:
        LOGGER.critical("Erreur fatale: %s", e, exc_info=True)
        sys.exit(1)

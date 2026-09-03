#!/usr/bin/env python3
# VINTED_API_AIOHTTP_V8_CATALOG_ONLY
# Scanner autonome : catalogue Vinted uniquement, sans appel détail par annonce.

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
from functools import lru_cache
from pathlib import Path

# ---------- Configuration ----------
ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("VINTED_DATA_DIR", str(ROOT / "runtime_data"))).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = ROOT / "config.json"
BLACKLIST_PATH = ROOT / "blacklist.json"
FILTRES_PATH = ROOT / "filtres.json"
TARGETS_PATH = ROOT / "produits_cibles.json"
SEEN_PATH = DATA_DIR / "annonces_vues.json"
SEEN_META_PATH = DATA_DIR / "annonces_vues_meta.json"
ALERTS_CSV = DATA_DIR / "alertes.csv"
SCAN_CURSOR_PATH = DATA_DIR / "scan_cursor.json"
CYCLE_STATE_PATH = DATA_DIR / "cycle_state.json"

ALERT_FIELDS = [
    "timestamp", "category", "product_type", "search", "brand", "model", "size",
    "opportunity_score", "title", "published_at", "age_minutes",
    "view_count", "favourite_count", "seller_type", "previous_price",
    "price_drop_pct", "image_url", "listing_price", "total_buy_est",
    "resale_low", "resale_high", "margin_low", "margin_high", "roi_low",
    "demand_score", "target_price", "price_zone", "risk", "reason", "url", "item_id",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
LOGGER = logging.getLogger("vinted_api_light")

SPACE_RE = re.compile(r"\s+")
NUMERIC_TIMESTAMP_RE = re.compile(r"\d+(?:\.\d+)?")
BUNDLE_COUNT_RE = re.compile(r"(?<!\d)(\d{1,2})(?!\d)")


@lru_cache(maxsize=8192)
def _normalise_cached(value):
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return SPACE_RE.sub(" ", value.lower()).strip()


@lru_cache(maxsize=8192)
def _term_regex(normalised_term):
    return re.compile(rf"(?<!\w){re.escape(normalised_term)}(?!\w)")

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
    return _normalise_cached(str(s or ""))


def term_present_normalized(normalised_text, term):
    normalised_term = norm(term)
    if not normalised_term:
        return False
    return _term_regex(normalised_term).search(normalised_text) is not None

def term_present(text, term):
    return term_present_normalized(norm(text), term)


def matching_terms(text, terms):
    """Normalise le texte une fois et déduplique les listes de mots."""
    normalised_text = norm(text)
    unique_terms = dict.fromkeys(str(term) for term in terms if str(term).strip())
    return [term for term in unique_terms
            if term_present_normalized(normalised_text, term)]

def product_type_from_category(category):
    """Convertit les catégories historiques en types de produits stricts."""
    category_n = norm(category).upper()
    if category_n.startswith("JEU_"):
        return "GAME"
    if category_n == "CONSOLE":
        return "CONSOLE"
    if category_n.startswith("ACCESS"):
        return "ACCESSORY"
    return "ELECTRONICS"

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
        if NUMERIC_TIMESTAMP_RE.fullmatch(raw):
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

def catalog_timestamp(item):
    """Date fiable du catalogue, avec repli sur la photo principale."""
    if not isinstance(item, dict):
        return None
    for key in ("created_at_ts", "created_at", "uploaded_at", "upload_date"):
        if item.get(key) is not None:
            return item[key]
    photos = item.get("photos") or []
    main_photo = item.get("photo")
    candidates = ([main_photo] if isinstance(main_photo, dict) else []) + [
        photo for photo in photos if isinstance(photo, dict)
    ]
    candidates.sort(key=lambda photo: not bool(photo.get("is_main")))
    for photo in candidates:
        high_resolution = photo.get("high_resolution") or {}
        timestamp = high_resolution.get("timestamp")
        if timestamp is not None:
            return timestamp
    return None

def freshness_check(value, cfg, now=None):
    max_age = float(cfg.get("max_listing_age_hours", 0.5))
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
    # Nouveau préfixe : les anciens échecs de l'API détail ne doivent pas
    # condamner définitivement des annonces qui n'ont jamais été analysées.
    return f"api3::search::{name}::{item_id}"

def alert_seen_key(item_id):
    return f"alert::{item_id}"

def evaluated_seen_key(item_id):
    return f"evaluated::{item_id}"

def mark_seen(seen_ids, key, seen_meta=None, now=None):
    key = str(key)
    seen_ids.add(key)
    if seen_meta is not None:
        seen_meta[key] = float(now if now is not None else time.time())

def item_already_seen(seen_ids, search, item_id):
    return (str(item_id) in seen_ids or
            search_seen_key(search, item_id) in seen_ids or
            alert_seen_key(item_id) in seen_ids or
            evaluated_seen_key(item_id) in seen_ids)

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

def save_cycle_state(seen_ids, seen_meta, cursor=0):
    """Une seule écriture atomique pour tout l'état persistant du cycle."""
    keys = sorted(str(k) for k in seen_ids)
    meta = {k: float(seen_meta.get(k, time.time())) for k in keys}
    save_json(CYCLE_STATE_PATH, {
        "schema": 1,
        "seen_ids": keys,
        "seen_meta": meta,
        "scan_cursor": int(cursor),
    })

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
    product_type = str(entry.get("type_produit", "")).strip().upper()
    if not product_type:
        product_type = product_type_from_category(category)
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
        "max_buy_ratio": float(entry.get("ratio_achat_max", 0.50)),
        "product_type": product_type,
        "profile_priority": 20,
    }
    if entry.get("materiel_dans_titre"):
        rule["hardware_in_title"] = True
    return [{
        "name": f"FILTRE - {name} / {query}",
        "category": category,
        "product_type": product_type,
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

def convert_target_product(entry):
    """Transforme une ligne lisible de produits_cibles.json en règle interne."""
    if not isinstance(entry, dict) or not entry.get("active", True):
        return None
    name = str(entry.get("name", "")).strip()
    product_type = str(entry.get("type", "")).strip().upper()
    query = str(entry.get("query") or name).strip()
    price_max = _positive_price(entry.get("price_max"))
    resale_low = _positive_price(entry.get("resale_low"))
    if not name or not product_type or not query or price_max is None or resale_low is None:
        return None
    category = str(entry.get("category", "")).strip()
    if not category:
        category = {
            "CONSOLE": "CONSOLE", "GAME": "JEU_AUTRE",
            "ACCESSORY": "ACCESSOIRE",
        }.get(product_type, "ELECTRONIQUE")
    rule = {
        "label": name,
        "brand": str(entry.get("brand", "")).strip(),
        "model": str(entry.get("model") or name).strip(),
        "product_type": product_type,
        "accessory_type": str(entry.get("accessory_type", "")).strip().upper(),
        "must_contain": list(entry.get("must", [])),
        "any_contain": list(entry.get("any", [])),
        "platform_any": list(entry.get("platform", [])),
        "title_prefix_any": list(entry.get("title_prefix", [])),
        "exclude": list(entry.get("exclude", [])),
        "resale_low": float(resale_low),
        "resale_high": float(entry.get("resale_high", resale_low)),
        "min_margin": float(entry.get("min_margin", 8)),
        "min_roi_pct": float(entry.get("min_roi_pct", 20)),
        "demand_score": int(entry.get("demand", 4)),
        "profile_priority": 10,
        "hot_buy_price": float(entry.get("hot_buy", price_max)),
        "suspicious_below": float(entry.get("suspicious_below", 0) or 0),
        "allow_loose": bool(entry.get("allow_loose", False)),
        "manual_review": bool(entry.get("manual_review", False)),
        "bundle_min_items": int(entry.get("bundle_min_items", 0) or 0),
    }
    return {
        "name": f"CIBLE - {name}",
        "category": category,
        "product_type": product_type,
        "query": query,
        "price_to": float(price_max),
        "rules": [rule],
    }

def load_target_products(path=TARGETS_PATH):
    """Charge la liste de prix séparément du code et ignore les lignes invalides."""
    try:
        data = load_json(path, {})
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error("Profil produits illisible: %s", exc)
        return []
    entries = data.get("products", []) if isinstance(data, dict) else []
    searches = []
    invalid = 0
    for entry in entries:
        search = convert_target_product(entry)
        if search is None:
            invalid += 1
        else:
            searches.append(search)
    if invalid:
        LOGGER.warning("Profil produits: %s lignes invalides ignorées", invalid)
    return searches

def _merge_searches(searches):
    """Fusionne les requêtes identiques pour économiser des appels HTTP."""
    merged = {}
    for search in searches:
        if not isinstance(search, dict) or not str(search.get("query", "")).strip():
            continue
        key = norm(str(search["query"]))
        if key not in merged:
            merged[key] = dict(search)
            merged[key]["rules"] = list(search.get("rules", []))
            continue
        current = merged[key]
        current["rules"].extend(search.get("rules", []))
        prices = [x for x in (current.get("price_to"), search.get("price_to")) if x is not None]
        current["price_to"] = max(float(x) for x in prices) if prices else None
    return list(merged.values())

def _collapse_personal_variants(searches):
    """Une requête précise par produit, au lieu de gaspiller trois créneaux."""
    regular = []
    families = {}
    for search in searches:
        name = str(search.get("name", ""))
        if not name.startswith("FILTRE - "):
            regular.append(search)
            continue
        family = name.split(" / ", 1)[0]
        previous = families.get(family)
        if previous is None or len(norm(search.get("query", ""))) > len(norm(previous.get("query", ""))):
            families[family] = search
    return list(families.values()) + regular

def select_searches_for_run(searches, cfg, cursor=None, persist_cursor=True):
    """Garde les recherches clés et fait tourner les autres à chaque cycle."""
    searches = _collapse_personal_variants(_merge_searches(searches))
    limit = max(1, int(cfg.get("max_searches_per_run", 10)))
    anchors = {norm(x) for x in cfg.get("always_search_queries", [])}
    fixed = [search for search in searches if norm(search.get("query", "")) in anchors]
    fixed = fixed[:limit]
    remaining = [search for search in searches if search not in fixed]
    slots = max(0, limit - len(fixed))
    if cursor is None:
        cursor_data = load_json(SCAN_CURSOR_PATH, {})
        try:
            cursor = (
                int(cursor_data.get("cursor", 0))
                if isinstance(cursor_data, dict) and cursor_data.get("schema") == 4
                else 0
            )
        except (TypeError, ValueError):
            cursor = 0
    else:
        try:
            cursor = int(cursor)
        except (TypeError, ValueError):
            cursor = 0
    rotating = []
    if remaining and slots:
        cursor %= len(remaining)
        rotating = [remaining[(cursor + index) % len(remaining)] for index in range(min(slots, len(remaining)))]
        cursor = (cursor + len(rotating)) % len(remaining)
        if persist_cursor:
            save_json(SCAN_CURSOR_PATH, {"schema": 4, "cursor": cursor})
    cfg["_next_scan_cursor"] = cursor
    return fixed + rotating

def candidate_rank(row):
    """Classe rentabilité, demande, fraîcheur et concurrence."""
    age_value = row.get("age_minutes")
    age = 999.0 if age_value in (None, "") else float(age_value)
    views = float(row.get("view_count") or 0)
    favourites = float(row.get("favourite_count") or 0)
    return (
        float(row.get("opportunity_score") or 0) * 100
        + float(row.get("demand_score") or 0) * 10
        + min(float(row.get("margin_low") or 0), 200) * 0.10
        + (35 if row.get("price_zone") == "JACKPOT" else 0)
        - age * 0.50
        - views * 0.05
        - favourites * 0.50
    )

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
    risk_suffix = f" | ATTENTION: {row['risk']}" if row.get("risk") else ""
    zone = " JACKPOT" if row.get("price_zone") == "JACKPOT" else ""
    body = (f"[{row['opportunity_score']}/10]{zone} {row['title']} | "
            f"Achat {row['listing_price']:.2f} EUR | revente "
            f"{row['resale_low']:.0f}-{row['resale_high']:.0f} EUR | "
            f"bénéfice prudent {row['margin_low']:.2f} EUR | {row['reason']}"
            f"{risk_suffix}")
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
def blacklist_check(title, text, blacklist, check_accessories=True,
                    ignored_accessory_terms=()):
    combined = f"{title} {text}"
    groups = ["hard_blacklist", "fake_blacklist"]
    if check_accessories:
        groups.extend(("accessory_blacklist", "title_accessory_blacklist"))
    ignored = {norm(word) for word in ignored_accessory_terms}
    for group in groups:
        if group == "title_accessory_blacklist":
            hits = [w for w in matching_terms(title, blacklist.get(group, []))
                    if norm(w) not in ignored]
        else:
            hits = [w for w in matching_terms(combined, blacklist.get(group, []))
                    if norm(w) not in ignored]
        if hits:
            return True, group, hits[:3], []
    risks = matching_terms(combined, blacklist.get("suspicious_words", []))
    return False, "", [], risks[:3]

EMPTY_PACKAGING_TERMS = frozenset((
    "boite vide", "boîte vide", "boitier vide", "boîtier vide",
    "empty box", "box only", "boite seule", "boîte seule",
    "juste la boite", "juste la boîte", "sans console", "sans jeu",
    "game case only", "coffret vide", "empty collector box",
))

CONSOLE_ACCESSORY_TERMS = frozenset((
    "manette", "manettes", "controller", "controllers", "mando", "mandos",
    "gamepad", "gamepads", "joy-con", "joycon", "joystick", "joysticks",
    "dock", "chargeur",
    "charger", "câble", "cable", "coque", "housse", "étui", "etui",
    "pochette", "support", "stand", "batterie", "écran", "ecran",
    "joystick", "lecteur seul", "carte mémoire", "carte memoire",
    "micro sd", "microsd", "adaptateur", "alimentation",
))

GAME_ACCESSORY_TERMS = frozenset((
    "steelbook", "poster", "figurine", "amiibo", "goodies", "artbook",
    "guide", "manuel seul", "manual only", "porte-clés", "porte cles",
    "code seul", "download code only", "jaquette seule", "cover only",
))

GAME_HARDWARE_TERMS = frozenset((
    "console", "pack console", "switch oled", "switch lite",
    "ps5 slim", "ps5 pro", "ps4 pro", "xbox series s", "xbox series x",
    "xbox one x", "3ds xl", "2ds xl", "game boy advance sp",
))

ELECTRONICS_ACCESSORY_TERMS = frozenset((
    "chargeur seul", "charger only", "câble seul", "cable only",
    "coque", "housse", "étui", "etui", "batterie seule", "battery only",
    "objectif seul", "lens only", "écran seul", "ecran seul",
    "adaptateur seul", "power adapter only", "boitier vide", "boîtier vide",
))

UNSAFE_CONDITION_TERMS = frozenset((
    "non testé", "non teste", "pas testé", "pas teste", "untested",
    "sans chargeur", "sans câble", "sans cable", "sans manette",
    "without charger", "without cable", "without controller",
    "jeu rayé", "jeu raye", "disque rayé", "disque raye",
    "cartouche abîmée", "cartouche abimee", "scratched disc",
))

LOOSE_GAME_TERMS = frozenset((
    "jeu sans boîte", "jeu sans boite", "cartouche nue",
    "cartouche seule", "loose cartridge", "disque nu", "disc only",
))

ACCESSORY_PART_TERMS = frozenset((
    "façade", "facade", "faceplate", "support manette", "controller stand",
    "coque manette", "controller shell", "boutons de rechange",
    "replacement buttons", "joystick de rechange", "replacement joystick",
    "pédalier seul", "pedal set only", "levier seul", "shifter only",
    "câble seul", "cable only", "adaptateur seul", "adapter only",
))

ACCESSORY_ONLY_MARKERS = frozenset((
    "pour", "para", "for", "fur", "für", "compatible", "seul", "seule",
    "only", "support", "stand", "housse", "coque", "pochette", "case",
))

PLATFORM_FAMILIES = {
    "SWITCH": frozenset(("switch", "nintendo switch")),
    "PS5": frozenset(("ps5", "playstation 5")),
    "PS4": frozenset(("ps4", "playstation 4")),
    "PS3": frozenset(("ps3", "playstation 3")),
    "PS2": frozenset(("ps2", "playstation 2")),
    "PS1": frozenset(("ps1", "playstation 1")),
    "XBOX": frozenset(("xbox", "xbox one", "xbox series")),
    "WII": frozenset(("wii", "wii u")),
    "3DS": frozenset(("3ds", "nintendo 3ds")),
    "DS": frozenset(("nintendo ds", "ds lite")),
    "N64": frozenset(("n64", "nintendo 64")),
    "SNES": frozenset(("snes", "super nintendo")),
    "GBA": frozenset(("gba", "game boy advance", "gameboy advance")),
    "GAME_BOY": frozenset(("game boy", "gameboy", "gbc")),
}

CONSOLE_INTENT_TERMS = frozenset((
    "console", "consola", "konsole", "pack console", "bundle console",
    "pack ps5", "bundle ps5", "pack switch", "bundle switch",
    "pack xbox", "bundle xbox",
))


def _platform_families_in(text):
    text_n = norm(text)
    return {
        family for family, aliases in PLATFORM_FAMILIES.items()
        if any(term_present_normalized(text_n, alias) for alias in aliases)
    }


def _platform_conflict(title, allowed_platforms):
    mentioned = _platform_families_in(title)
    wanted = _platform_families_in(" ".join(str(x) for x in allowed_platforms))
    return bool(mentioned and wanted and mentioned.isdisjoint(wanted))


def _starts_with_term(title, terms):
    title_n = norm(title)
    return any(
        title_n == norm(term) or title_n.startswith(norm(term) + " ")
        for term in terms
    )

def infer_product_type(source_search, rule):
    """Retourne le type explicite, ou l'infère sans modifier config.json."""
    explicit = str(
        rule.get("product_type") or source_search.get("product_type") or ""
    ).strip().upper()
    aliases = {
        "JEU": "GAME", "JEUX": "GAME", "GAME": "GAME",
        "CONSOLE": "CONSOLE",
        "CALCULATRICE": "CALCULATOR", "CALCULATOR": "CALCULATOR",
        "APPAREIL_PHOTO": "CAMERA", "CAMERA": "CAMERA",
        "MINI_PC": "MINI_PC", "MINIPC": "MINI_PC",
        "AUDIO": "AUDIO", "ELECTRONIQUE": "ELECTRONICS",
        "ELECTRONICS": "ELECTRONICS", "ACCESSOIRE": "ACCESSORY",
        "ACCESSORY": "ACCESSORY",
    }
    if explicit:
        return aliases.get(explicit, explicit)
    category_type = product_type_from_category(source_search.get("category", ""))
    if category_type != "ELECTRONICS":
        return category_type
    identity = norm(f"{rule.get('label', '')} {rule.get('model', '')}")
    if any(term_present(identity, word) for word in ("ti-84", "ti 84", "nspire", "calculatrice")):
        return "CALCULATOR"
    if any(term_present(identity, word) for word in ("appareil photo", "camera", "a6000", "g7 x")):
        return "CAMERA"
    if any(term_present(identity, word) for word in ("mini pc", "beelink", "minisforum")):
        return "MINI_PC"
    if any(term_present(identity, word) for word in ("walkman", "cassette")):
        return "AUDIO"
    return "ELECTRONICS"

def strict_product_type_check(source_search, rule, title, cfg=None):
    """Bloque seulement les incompatibilités certaines entre produit et titre."""
    if cfg is not None and not cfg.get("strict_product_type", True):
        return True, ""
    if any(term_present(title, word) for word in EMPTY_PACKAGING_TERMS):
        return False, "emballage vide"

    product_type = infer_product_type(source_search, rule)
    if product_type == "CONSOLE":
        has_console_word = any(term_present(title, word) for word in (
            "console", "consola", "konsole",
        ))
        game_words = (
            "jeu", "jeux", "game", "games", "juego", "juegos",
            "gioco", "giochi", "spiel", "spiele", "spel", "jogo", "jogos",
        )
        if any(term_present(title, word) for word in game_words) and not has_console_word:
            return False, "jeu, pas console"
        accessory_hit = any(term_present(title, word) for word in CONSOLE_ACCESSORY_TERMS)
        accessory_only = (
            _starts_with_term(title, CONSOLE_ACCESSORY_TERMS)
            or any(term_present(title, word) for word in ACCESSORY_ONLY_MARKERS)
        )
        if accessory_hit and not has_console_word and accessory_only:
            return False, "accessoire, pas console"
    elif product_type == "GAME":
        if any(term_present(title, word) for word in GAME_ACCESSORY_TERMS):
            return False, "accessoire de jeu"
        if any(term_present(title, word) for word in GAME_HARDWARE_TERMS):
            return False, "console, pas jeu"
        platforms = rule.get("platform_any", [])
        if platforms and _platform_conflict(title, platforms):
            return False, "plateforme différente"
        minimum = int(rule.get("bundle_min_items", 0) or 0)
        if minimum:
            counts = [int(value) for value in BUNDLE_COUNT_RE.findall(norm(title))]
            if not counts or max(counts) < minimum:
                return False, "nombre de jeux du lot non confirmé"
    elif product_type == "ACCESSORY":
        if any(term_present(title, word) for word in ACCESSORY_PART_TERMS):
            return False, "pièce d'accessoire seulement"
        subtype = str(rule.get("accessory_type", "")).upper()
        if subtype == "CONTROLLER" and any(
                term_present(title, word) for word in ("dock", "chargeur", "station de charge")):
            return False, "chargeur, pas manette"
        if subtype == "DOCK" and any(
                term_present(title, word) for word in ("câble", "cable", "chargeur", "adaptateur")):
            return False, "câble, pas dock"
        if subtype == "WHEEL" and any(
                term_present(title, word) for word in ("jeu", "game", "support volant")):
            return False, "jeu ou support, pas volant"
    else:
        if any(term_present(title, word) for word in ELECTRONICS_ACCESSORY_TERMS):
            return False, "accessoire électronique"
    return True, ""


def soft_filter_risks(source_search, rule, title, blacklist=None):
    """Transforme les ambiguïtés en avertissements au lieu de perdre l'annonce."""
    risks = []
    unsafe = matching_terms(title, UNSAFE_CONDITION_TERMS)
    if unsafe:
        risks.append("état ou équipement à vérifier: " + ", ".join(unsafe[:2]))

    prefixes = [norm(value) for value in rule.get("title_prefix_any", []) if norm(value)]
    title_n = norm(title)
    if prefixes and not any(
            title_n == prefix or title_n.startswith(prefix + " ")
            for prefix in prefixes):
        risks.append("titre ambigu: vérifier que le produit complet est inclus")

    if infer_product_type(source_search, rule) == "GAME":
        platforms = rule.get("platform_any", [])
        if (platforms and not _platform_conflict(title, platforms)
                and not any(term_present(title, platform) for platform in platforms)):
            risks.append("plateforme non indiquée dans le titre")

    if blacklist:
        suspicious = matching_terms(title, blacklist.get("suspicious_words", []))
        if suspicious:
            risks.append("annonce à contrôler: " + ", ".join(suspicious[:2]))
    return list(dict.fromkeys(risks))

def rule_match(rule, title, text, deep=False):
    title_n = norm(title)
    full = norm(f"{title} {text}")
    must = frozenset(rule.get("must_contain", []))
    any_kw = frozenset(rule.get("any_contain", []))
    exclude = frozenset(rule.get("exclude", []))
    if must and not all(term_present_normalized(title_n, word) for word in must):
        return False
    if any_kw and not any(term_present_normalized(title_n, word) for word in any_kw):
        return False
    if exclude and any(term_present_normalized(full, word) for word in exclude):
        return False
    if not deep:
        return True
    platform = rule.get("platform_any", [])
    hardware = rule.get("hardware_any", [])
    if platform and not any(term_present_normalized(full, word) for word in platform):
        return False
    if hardware:
        hardware_text = title_n if rule.get("hardware_in_title") else full
        if not any(term_present_normalized(hardware_text, word) for word in hardware):
            return False
    return True

def build_rule_index(searches, cfg):
    """Index mondial des produits connus à forte demande."""
    minimum_demand = int(cfg.get("min_demand_score", 4))
    index = []
    seen = set()
    for source_search in searches:
        for rule in source_search.get("rules", []):
            if int(rule.get("demand_score", 0)) < minimum_demand:
                continue
            identity = (
                norm(rule.get("brand", "")), norm(rule.get("model", "")),
                tuple(norm(x) for x in rule.get("must_contain", [])),
            )
            if identity in seen:
                continue
            seen.add(identity)
            index.append((source_search, rule))
    return index

def choose_known_product(rule_index, title, price, cfg=None):
    """Reconnaît le meilleur produit rentable dans une annonce découverte."""
    title_matches = []
    for source_search, rule in rule_index:
        if not rule_match(rule, title, title, deep=False):
            continue
        type_ok, _ = strict_product_type_check(
            source_search, rule, title, cfg,
        )
        if not type_ok:
            continue
        title_matches.append((
            infer_product_type(source_search, rule), source_search, rule,
        ))
    if not title_matches:
        return None, None

    # La classification se fait avant le filtre de prix. Ainsi un jeu PS5 trop
    # cher n'est jamais recyclé en fausse « console PS5 à 20 EUR ».
    has_console_intent = any(
        term_present(title, term) for term in CONSOLE_INTENT_TERMS
    )
    matched_types = {item[0] for item in title_matches}
    if "GAME" in matched_types and not has_console_intent:
        title_matches = [item for item in title_matches if item[0] == "GAME"]
    elif "CONSOLE" in matched_types and has_console_intent:
        title_matches = [item for item in title_matches if item[0] == "CONSOLE"]

    matches = []
    for product_type, source_search, rule in title_matches:
        price_limit = source_search.get("price_to")
        resale_low = rule.get("resale_low")
        ratio = float(rule.get("max_buy_ratio", 0.50))
        ratio_limit = float(resale_low) * ratio if resale_low is not None else None
        effective_limit = price_limit if price_limit is not None else ratio_limit
        if effective_limit is not None and float(price) > float(effective_limit):
            continue
        priority = (
            int(rule.get("profile_priority", 0)),
            int(rule.get("demand_score", 0)),
            len(rule.get("must_contain", [])) + len(rule.get("any_contain", [])),
            float(rule.get("resale_low") or 0) - float(price),
        )
        matches.append((priority, product_type, source_search, rule))
    if not matches:
        return None, None

    _, _, source_search, rule = max(matches, key=lambda item: item[0])
    return source_search, rule

# ---------- Appels API ----------
async def catalog_items(query, price_to, base_url, limiter, session, headers,
                        per_page=50, stats=None):
    request_started = time.perf_counter()
    url = f"{base_url}/api/v2/catalog/items"
    params = {
        "search_text": query,
        "order": "newest_first",
        "per_page": max(1, min(int(per_page), 50)),
    }
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
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
        LOGGER.error("Erreur catalogue %s: %s", query, exc)
        return []
    finally:
        if stats is not None:
            stats["catalog_seconds"] += time.perf_counter() - request_started

# ---------- Scan principal ----------
async def scan_search(search, cfg, blacklist, seen_ids, seen_meta,
                      limiter, session, base_url, headers, stats,
                      rule_index=None):
    query = search["query"]
    name = search.get("name", query)
    LOGGER.info(f"\n[API-TEST] {name} → {query}")

    max_items = min(
        int(search.get("max_items", cfg.get("max_items_per_search", 50))),
        int(cfg.get("max_items_per_search", 50)),
    )
    items = await catalog_items(
        query, search.get("price_to"), base_url, limiter, session,
        headers, per_page=int(cfg.get("catalog_per_page", 50)), stats=stats,
    )
    if not items:
        return []

    alerts = []

    for item in items[:max_items]:
        stats["items_examined"] += 1
        item_id = str(item.get("id"))
        title = item.get("title", "")
        price = _positive_price(item.get("price"))
        if price is None:
            stats["rejected_price"] += 1
            continue

        # 1. Vérifier âge
        created = catalog_timestamp(item)
        # Aucun appel détail : l'âge provient uniquement du catalogue/photo.
        age = listing_age_hours(created)
        if age is None:
            stats["age_unknown"] += 1
            LOGGER.warning("  ? Âge catalogue introuvable | %s", item_id)
            if cfg.get("reject_unknown_listing_age", True):
                continue
            age = float(cfg.get("max_listing_age_hours", 0.5))
        stats["age_known"] += 1
        if age > float(cfg.get("max_listing_age_hours", 0.5)):
            stats["rejected_old"] += 1
            LOGGER.debug("  X Âge | %.1fh", age)
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        # 2. Vérifier vu (sauf si baisse de prix, simplifié ici)
        if item_already_seen(seen_ids, search, item_id):
            stats["rejected_seen"] += 1
            continue

        # 3. Vendeur pro : blocage uniquement si explicitement demandé.
        seller = item.get("user", {})
        if cfg.get("exclude_professional_sellers", True) and (seller.get("is_business") or seller.get("is_pro")):
            stats["rejected_pro"] += 1
            LOGGER.debug("  X Vendeur Pro | %s", title[:60])
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        # 4. Filtres rapides
        blocked, _, _, _ = blacklist_check(
            title, title, blacklist, check_accessories=False,
        )
        if blocked:
            stats["rejected_blacklist"] += 1
            LOGGER.debug("  X Blacklist | %s", title[:60])
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        # 5. Trouver la règle
        if rule_index is not None:
            matched_search, matched_rule = choose_known_product(
                rule_index, title, price, cfg,
            )
        else:
            matched_search = search
            matched_rule = next(
                (rule for rule in search.get("rules", [])
                 if rule_match(rule, title, title)
                 and strict_product_type_check(search, rule, title, cfg)[0]),
                None,
            )
        if matched_rule is None or matched_search is None:
            stats["rejected_rule"] += 1
            continue

        category = matched_search.get("category", "")
        product_type = infer_product_type(matched_search, matched_rule)
        # Les accessoires et ambiguïtés ne déclenchent plus un second rejet
        # global : la règle de type a déjà éliminé les incompatibilités sûres.
        filter_risks = soft_filter_risks(
            matched_search, matched_rule, title, blacklist,
        )

        # 6. Scoring avec catalogue
        total, resale_low, resale_high, margin_low, margin_high, roi_low = score_candidate(matched_rule, price, cfg)
        if margin_low is None:
            stats["rejected_profit"] += 1
            continue
        ref_price = float(matched_rule.get("market_avg", resale_low))
        min_margin = matched_rule.get("min_margin", cfg.get("min_margin", 25))
        min_roi = matched_rule.get("min_roi_pct", cfg.get("min_roi_pct", 20))
        strict_profit = margin_low >= float(min_margin) and roi_low >= float(min_roi)
        if (margin_low < float(cfg.get("candidate_min_margin", 8)) or
                roi_low < float(cfg.get("candidate_min_roi_pct", 20))):
            stats["rejected_profit"] += 1
            continue

        # Le catalogue fournit déjà prix, vues, favoris, vendeur et photo.
        # L'endpoint détail public temporise/échoue sur GitHub Actions : ne pas
        # le laisser bloquer les alertes.
        fav_count = item.get("favourite_count")
        view_count = item.get("view_count")
        description = item.get("description", "")

        # 8. Score final
        motivation_hits = matching_terms(
            f"{title} {description}", cfg.get("seller_motivation_words", []),
        )
        score = opportunity_score(
            price, ref_price, margin_low, motivation_hits,
            age_hours=age, favourite_count=fav_count,
            view_count=view_count, cfg=cfg,
        )
        hot_buy_price = float(matched_rule.get("hot_buy_price", 0) or 0)
        price_zone = "JACKPOT" if hot_buy_price and price <= hot_buy_price else "CIBLE"
        if price_zone == "JACKPOT":
            score = min(10, score + 1)
        score -= min(
            float(cfg.get("soft_risk_penalty_cap", 1.0)),
            len(filter_risks) * float(cfg.get("soft_risk_penalty", 0.35)),
        )
        score = round(max(1.0, score), 1)
        if score < int(cfg.get("min_candidate_score", 5)):
            stats["rejected_score"] += 1
            continue

        # 9. Alerte
        size = "?"
        published_dt = parse_vinted_timestamp(created)
        published_at = published_dt.isoformat(timespec="seconds") if published_dt else ""

        risk_parts = list(filter_risks)
        if seller.get("is_business") or seller.get("is_pro"):
            risk_parts.append("vendeur professionnel")
        suspicious_below = float(matched_rule.get("suspicious_below", 0) or 0)
        if suspicious_below and price <= suspicious_below:
            risk_parts.append("prix anormalement bas: vérifier vendeur et contenu")
        if matched_rule.get("manual_review"):
            risk_parts.append("contenu à vérifier manuellement")
        if not strict_profit:
            risk_parts.append("seuil prudent à vérifier")

        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "category": category,
            "product_type": product_type,
            "search": search.get("name", "") + " → " + matched_search.get("name", "")
                      + " / " + matched_rule.get("label", ""),
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
            "target_price": matched_search.get("price_to", ""),
            "price_zone": price_zone,
            "risk": "; ".join(risk_parts),
            "reason": reason_text(
                price, ref_price, motivation_hits, age_hours=age,
                cfg=cfg, favourite_count=fav_count,
                view_count=view_count,
            ),
            "url": f"{base_url}/items/{item_id}",
            "item_id": item_id,
            "_seen_key": search_seen_key(search, item_id),
        }

        alerts.append(row)
        stats["candidates"] += 1
        LOGGER.info(
            "  + CANDIDAT %s/10 | %s | %s | %.2f EUR | marge +%.2f EUR",
            score, freshness_label(age, cfg), title[:58], price, margin_low,
        )

    return alerts

# ---------- Main ----------
async def main_async():
    cycle_started = time.perf_counter()
    cfg = load_json(CONFIG_PATH, {})
    if not cfg:
        LOGGER.error("config.json introuvable")
        return

    blacklist = load_json(BLACKLIST_PATH, {})
    target_searches = load_target_products()
    cfg["searches"] = target_searches + list(cfg.get("searches", []))
    LOGGER.info("Profil marché: %s produits cibles chargés", len(target_searches))
    personal_count = apply_personal_filters(cfg, blacklist)
    LOGGER.info("%s recherches personnelles ajoutées", personal_count)
    product_searches = list(cfg.get("searches", []))
    rule_index = build_rule_index(product_searches, cfg)
    LOGGER.info("Profil demande chargé | %s produits reconnus", len(rule_index))

    # Charger l'état agrégé, avec migration automatique de l'ancien format.
    seen_ids = set()
    seen_meta = {}
    cycle_state = load_json(CYCLE_STATE_PATH, {})
    scan_cursor = 0
    if isinstance(cycle_state, dict) and cycle_state.get("schema") == 1:
        seen_ids = {str(x) for x in cycle_state.get("seen_ids", [])}
        raw_meta = cycle_state.get("seen_meta", {})
        seen_meta = raw_meta if isinstance(raw_meta, dict) else {}
        try:
            scan_cursor = int(cycle_state.get("scan_cursor", 0))
        except (TypeError, ValueError):
            scan_cursor = 0
    elif SEEN_PATH.exists():
        raw = load_json(SEEN_PATH, [])
        if isinstance(raw, list):
            seen_ids = {str(x) for x in raw}
    if not seen_meta and SEEN_META_PATH.exists():
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
        "catalog_seconds", "items_examined", "searches_completed",
        "searches_failed", "search_seconds", "age_known", "age_unknown",
        "notifications_sent", "candidates", "rejected_price",
        "rejected_old", "rejected_seen", "rejected_pro",
        "rejected_blacklist", "rejected_rule", "rejected_profit",
        "rejected_score",
    )}

    concurrency = max(1, int(cfg.get("api_max_concurrency", 3)))
    connector = aiohttp.TCPConnector(
        limit=max(4, concurrency * 2), ttl_dns_cache=300,
    )
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    async with aiohttp.ClientSession(
            connector=connector, timeout=timeout) as session:
        # Crée les cookies de session avant l'API. Un échec ici est informatif,
        # mais les appels catalogue décideront de l'état de santé réel.
        try:
            async with session.get(base_url + "/", headers={
                **headers, "Accept": "text/html,application/xhtml+xml"
            }, timeout=12) as resp:
                LOGGER.info("Initialisation session HTTP %s", resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            LOGGER.warning("Initialisation session impossible: %s", exc)

        discovery_mode = bool(cfg.get("discovery_mode", True))
        all_searches = (
            list(cfg.get("discovery_searches", []))
            if discovery_mode
            else product_searches
        )
        searches = select_searches_for_run(
            all_searches, cfg, cursor=scan_cursor, persist_cursor=False,
        )
        max_catalog_items = int(cfg.get("max_catalog_items_per_run", 50))
        per_search = max(1, max_catalog_items // max(1, len(searches)))
        cfg["max_items_per_search"] = min(
            int(cfg.get("max_items_per_search", 5)), per_search,
        )
        LOGGER.info(
            "Cycle rapide | %s/%s recherches | maximum %s annonces",
            len(searches), len(_collapse_personal_variants(_merge_searches(all_searches))),
            len(searches) * cfg["max_items_per_search"],
        )
        semaphore = asyncio.Semaphore(concurrency)

        async def bounded_scan(search):
            async with semaphore:
                search_started = time.perf_counter()
                try:
                    rows = await scan_search(
                        search, cfg, blacklist, seen_ids, seen_meta,
                        limiter, session, base_url, headers, stats,
                        rule_index=rule_index if discovery_mode else None,
                    )
                    stats["searches_completed"] += 1
                    return rows
                finally:
                    elapsed = time.perf_counter() - search_started
                    stats["search_seconds"] += elapsed
                    LOGGER.info(
                        "PERF recherche | %.2fs | %s",
                        elapsed, search.get("query", "?"),
                    )

        tasks = [bounded_scan(s) for s in searches]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        candidates = []
        for res in results:
            if isinstance(res, Exception):
                stats["searches_failed"] += 1
                LOGGER.error("Erreur recherche: %s", res)
            else:
                candidates.extend(res)

        # Une même annonce peut apparaître dans plusieurs recherches. On ne
        # conserve que sa meilleure évaluation avant de calculer le Top 5.
        best_by_item = {}
        for row in candidates:
            item_id = str(row.get("item_id"))
            previous = best_by_item.get(item_id)
            if previous is None or candidate_rank(row) > candidate_rank(previous):
                best_by_item[item_id] = row
        candidates = sorted(
            best_by_item.values(), key=candidate_rank, reverse=True,
        )
        selected = candidates[:max(1, int(cfg.get("max_alerts_per_run", 5)))]
        selected_ids = {str(row.get("item_id")) for row in selected}

        # Les candidats non retenus ont été analysés mais ne doivent pas
        # encombrer les cycles suivants.
        for row in candidates:
            if str(row.get("item_id")) not in selected_ids:
                mark_seen(seen_ids, evaluated_seen_key(row["item_id"]), seen_meta)

        for rank, row in enumerate(selected, start=1):
            LOGGER.info(
                "TOP %s | score %.1f | %s | %s",
                rank, candidate_rank(row), row["title"][:60], row["url"],
            )
            if await ntfy_send(row, session):
                clean_row = {key: value for key, value in row.items() if not key.startswith("_")}
                append_alert(clean_row)
                stats["notifications_sent"] += 1
                mark_seen(seen_ids, alert_seen_key(row["item_id"]), seen_meta)
                mark_seen(seen_ids, evaluated_seen_key(row["item_id"]), seen_meta)
                mark_seen(seen_ids, row["_seen_key"], seen_meta)
            else:
                LOGGER.error("Notification échouée, annonce conservée pour nouvel essai: %s", row["item_id"])

        total_alerts = len(selected)

        state_started = time.perf_counter()
        save_cycle_state(
            seen_ids, seen_meta, cfg.get("_next_scan_cursor", scan_cursor),
        )
        state_seconds = time.perf_counter() - state_started
        cycle_seconds = time.perf_counter() - cycle_started
        throughput = stats["items_examined"] / max(cycle_seconds, 0.001)
        LOGGER.info("Santé API | catalogues %s/%s | articles reçus %s | "
                    "articles analysés %s | âges connus %s | inconnus %s | notifications %s",
                    stats["catalog_success"], stats["catalog_requested"],
                    stats["catalog_items"], stats["items_examined"],
                    stats["age_known"], stats["age_unknown"],
                    stats["notifications_sent"])
        LOGGER.info(
            "Rejets | anciens %s | déjà vus %s | règle %s | rentabilité %s | "
            "score %s | filtres durs %s | pro %s",
            stats["rejected_old"], stats["rejected_seen"],
            stats["rejected_rule"], stats["rejected_profit"],
            stats["rejected_score"], stats["rejected_blacklist"], stats["rejected_pro"],
        )
        LOGGER.info(
            "\n[API] Terminé : %s candidats, Top %s, %s notifications envoyées.",
            len(candidates), total_alerts, stats["notifications_sent"],
        )
        LOGGER.info(
            "PERF cycle | %.2fs | %.1f articles/s | réseau cumulé %.2fs | "
            "état %.3fs (1 sauvegarde) | recherches %s OK / %s erreur(s)",
            cycle_seconds, throughput, stats["catalog_seconds"], state_seconds,
            stats["searches_completed"], stats["searches_failed"],
        )

        requested = stats["catalog_requested"]
        if requested and stats["catalog_success"] / requested < 0.5:
            raise RuntimeError("Moins de 50% des catalogues ont répondu: scan invalide")
        if requested and stats["catalog_success"] and stats["catalog_items"] == 0:
            raise RuntimeError("Catalogues vides: scan probablement bloqué par Vinted")
        sampled_ages = stats["age_known"] + stats["age_unknown"]
        if sampled_ages >= 3 and stats["age_known"] == 0:
            raise RuntimeError("Aucun âge lisible dans le catalogue: scan invalide")

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        LOGGER.info("Arrêt demandé")

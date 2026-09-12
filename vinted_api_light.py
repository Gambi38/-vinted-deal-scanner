#!/usr/bin/env python3
# VINTED_API_AIOHTTP_V17_ADAPTIVE_COVERAGE
# Scanner autonome : catalogue Vinted uniquement, sans appel détail par annonce.

import asyncio
import aiohttp
import csv
import json
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from monitoring import build_workflow_report
from photo_condition import PhotoConditionAnalyzer, enrich_rows_with_photos
from pricecharting_catalog import ReferenceCatalog
from product_classifier import (
    CLASSIFIER_SCHEMA,
    build_rule_vocabulary,
    canonicalize_product_title,
    vocabulary_size,
)
from scanner_runtime import (
    ApiBudgetExceeded,
    ApiCostController,
    TokenBucketRateLimiter,
    dns_healthcheck,
    managed_http_session,
    run_async,
)
from search_cache import load_rule_index, save_rule_index, search_fingerprint
from sold_listings import SoldListingsProvider
from universal_catalog import DeviceCatalog

# ---------- Configuration ----------
ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("VINTED_DATA_DIR", str(ROOT / "runtime_data"))).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = ROOT / "config.json"
BLACKLIST_PATH = ROOT / "blacklist.json"
FILTRES_PATH = ROOT / "filtres.json"
TARGETS_PATH = ROOT / "produits_cibles.json"
CONSOLES_TARGETS_PATH = ROOT / "consoles_cibles.json"
ELECTRONICS_TARGETS_PATH = ROOT / "electroniques_cibles.json"
SEEN_PATH = DATA_DIR / "annonces_vues.json"
SEEN_META_PATH = DATA_DIR / "annonces_vues_meta.json"
ALERTS_CSV = DATA_DIR / "alertes.csv"
SCAN_CURSOR_PATH = DATA_DIR / "scan_cursor.json"
CYCLE_STATE_PATH = DATA_DIR / "cycle_state.json"
PRICE_HISTORY_PATH = DATA_DIR / "annonces_prix.json"
SEARCH_CACHE_PATH = DATA_DIR / "searches_cache.json"
SOLD_LISTINGS_PATH = DATA_DIR / "sold_listings_cache.json"
REPORT_PATH = DATA_DIR / "rapport.json"
SEARCH_PERFORMANCE_PATH = DATA_DIR / "search_performance.json"
RATE_STATE_PATH = DATA_DIR / "rate_state.json"
PRICECHARTING_CATALOG_PATH = DATA_DIR / "pricecharting_catalog.json"
DEVICE_CATALOG_PATH = DATA_DIR / "device_catalog.json"
CONFIRMED_REJECTIONS_PATH = ROOT / "rejets.txt"

ALERT_FIELDS = [
    "timestamp", "category", "product_type", "search", "brand", "model", "size",
    "catalog_description", "reference_source", "sales_volume", "match_confidence",
    "opportunity_score", "title", "published_at", "age_minutes",
    "view_count", "favourite_count", "seller_type", "previous_price",
    "price_drop_pct", "image_url", "listing_price", "total_buy_est",
    "resale_low", "resale_high", "margin_low", "margin_high", "roi_low",
    "demand_score", "target_price", "price_zone", "photo_condition",
    "photo_confidence", "photo_risk", "risk", "reason", "url", "item_id",
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


def apply_confirmed_rejections(path, blacklist):
    """Ajoute uniquement des rejets explicitement classés par l'utilisateur."""
    mapping = {
        "hard": "hard_blacklist",
        "fake": "fake_blacklist",
        "accessory": "accessory_blacklist",
        "title": "title_accessory_blacklist",
        "payment": "off_platform_payment_blacklist",
    }
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    added = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        prefix, term = line.split(":", 1)
        group = mapping.get(prefix.strip().lower())
        term = term.strip()
        if not group or len(term) < 3:
            continue
        values = blacklist.setdefault(group, [])
        if term not in values:
            values.append(term)
            added += 1
    return added

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

# ---------- Compatibilité de l'ancien nom ----------
class AsyncRateLimiter(TokenBucketRateLimiter):
    """Ancien constructeur conservé pour les installations existantes."""

    def __init__(self, min_sec, max_sec, max_backoff=60):
        maximum = max(float(min_sec), float(max_sec), 0.05)
        super().__init__(
            capacity=1,
            refill_per_second=1.0 / maximum,
            min_jitter=float(min_sec),
            max_jitter=float(max_sec),
            max_backoff=float(max_backoff),
        )

# ---------- Gestion état ----------
def search_seen_key(search, item_id):
    name = norm(str(search.get("name") or search.get("query") or "search"))
    # Nouveau préfixe V13 : les annonces rejetées sous l'ancienne fenêtre de
    # 30 minutes sont réévaluées une fois avec la fenêtre de 3 heures.
    # Les alertes déjà envoyées restent dédupliquées par alert_seen_key().
    return f"api4::search::{name}::{item_id}"

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

def prune_price_history(price_history, retention_days=30, now=None):
    current = float(now if now is not None else time.time())
    cutoff = current - max(1, int(retention_days)) * 86400
    for item_id in list(price_history):
        row = price_history.get(item_id)
        try:
            updated_at = float(row.get("updated_at", 0)) if isinstance(row, dict) else 0
        except (TypeError, ValueError):
            updated_at = 0
        if updated_at < cutoff:
            price_history.pop(item_id, None)


def price_drop_event(price_history, item_id, price, cfg, title="", now=None):
    """Met à jour l'historique et renvoie (ancien prix, baisse %, événement)."""
    item_id = str(item_id)
    current = float(now if now is not None else time.time())
    previous_row = price_history.get(item_id, {})
    previous_price = _positive_price(
        previous_row.get("price") if isinstance(previous_row, dict) else None
    )
    drop_pct = 0.0
    if previous_price and price < previous_price:
        drop_pct = (previous_price - price) / previous_price * 100
    threshold = max(1.0, float(cfg.get("price_drop_alert_pct", 20)))
    price_history[item_id] = {
        "price": round(float(price), 2),
        "updated_at": current,
        "title": str(title)[:180],
    }
    return previous_price, round(drop_pct, 1), drop_pct >= threshold


def save_cycle_state(seen_ids, seen_meta, cursor=0, price_history=None):
    """Une seule écriture atomique pour tout l'état persistant du cycle."""
    keys = sorted(str(k) for k in seen_ids)
    meta = {k: float(seen_meta.get(k, time.time())) for k in keys}
    save_json(CYCLE_STATE_PATH, {
        "schema": 1,
        "seen_ids": keys,
        "seen_meta": meta,
        "scan_cursor": int(cursor),
    })
    if price_history is not None:
        save_json(PRICE_HISTORY_PATH, price_history)

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
        converted = convert_personal_filter(entry)
        if (not cfg.get("include_personal_console_filters", False)
                and any(infer_product_type(search, rule) == "CONSOLE"
                        for search in converted for rule in search.get("rules", []))):
            LOGGER.info(
                "Filtre console personnel ignoré au profit du profil contrôlé: %s",
                entry.get("nom", "sans nom"),
            )
            continue
        searches.extend(converted)
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
    supported_types = {
        "CONSOLE", "GAME", "ACCESSORY", "ELECTRONICS", "CALCULATOR",
        "CAMERA", "ACTION_CAMERA", "MINI_PC", "AUDIO", "EREADER",
        "STREAMING", "SMARTWATCH", "DRAWING_TABLET",
    }
    if (not name or product_type not in supported_types
            or not query or price_max is None or resale_low is None):
        return None
    category = str(entry.get("category", "")).strip()
    if not category:
        category = {
            "CONSOLE": "CONSOLE", "GAME": "JEU_AUTRE",
            "ACCESSORY": "ACCESSOIRE",
        }.get(product_type, "ELECTRONIQUE")
    exclusions = list(entry.get("exclude", []))
    if product_type == "CONSOLE":
        bundle_words = {
            "jeu", "jeux", "game", "games", "manette", "manettes",
            "controller", "controllers",
        }
        exclusions = [word for word in exclusions if norm(word) not in bundle_words]
    description = str(entry.get("description", "")).strip()
    if not description:
        description = {
            "CONSOLE": f"Console {name} complète et fonctionnelle.",
            "GAME": f"Jeu original {name}; vérifier la plateforme et l'état.",
            "ACCESSORY": f"Accessoire original {name}; vérifier qu'il est complet.",
        }.get(
            product_type,
            f"{name} complet et fonctionnel; vérifier le modèle et l'état.",
        )
    rule = {
        "label": name,
        "brand": str(entry.get("brand", "")).strip(),
        "model": str(entry.get("model") or name).strip(),
        "description": description,
        "product_type": product_type,
        "accessory_type": str(entry.get("accessory_type", "")).strip().upper(),
        "must_contain": list(entry.get("must", [])),
        "any_contain": list(entry.get("any", [])),
        "platform_any": list(entry.get("platform", [])),
        "title_prefix_any": list(entry.get("title_prefix", [])),
        "exclude": exclusions,
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
        "identity_any": list(entry.get("identity_any", [])),
        "match_groups": list(entry.get("match_groups", [])),
        "match_threshold": float(entry.get("match_threshold", 1.0)),
        "min_match_groups": int(entry.get("min_match_groups", 0) or 0),
        "external_source": str(entry.get("reference_source", "catalogue contrôlé")),
    }
    return {
        "name": f"CIBLE - {name}",
        "category": category,
        "product_type": product_type,
        "query": query,
        "price_to": float(price_max),
        "rules": [rule],
    }

def load_target_products(path=TARGETS_PATH, console_path=None, electronics_path=None):
    """Charge le catalogue contrôlé, avec priorité aux fichiers dédiés.

    produits_cibles.json reste compatible. Lors du chargement normal, une
    console portant le même nom dans consoles_cibles.json remplace sa version
    générale : il n'existe donc qu'un seul prix de référence par modèle.
    """
    main_path = Path(path)
    paths = [main_path]
    if main_path.resolve() == TARGETS_PATH.resolve():
        dedicated_paths = [
            Path(console_path) if console_path else CONSOLES_TARGETS_PATH,
            Path(electronics_path) if electronics_path else ELECTRONICS_TARGETS_PATH,
        ]
        for dedicated in reversed(dedicated_paths):
            if dedicated.exists():
                paths.insert(0, dedicated)
    searches_by_identity = {}
    invalid = 0
    for profile_path in paths:
        try:
            data = load_json(profile_path, {})
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.error("Profil produits illisible (%s): %s", profile_path.name, exc)
            continue
        entries = data.get("products", []) if isinstance(data, dict) else []
        for entry in entries:
            search = convert_target_product(entry)
            if search is None:
                invalid += 1
                continue
            rule = search["rules"][0]
            identity = (search["product_type"], norm(rule.get("model", "")))
            # Le fichier dédié est parcouru en premier et reste prioritaire.
            searches_by_identity.setdefault(identity, search)
    if invalid:
        LOGGER.warning("Profil produits: %s lignes invalides ignorées", invalid)
    return list(searches_by_identity.values())

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


def interleave_precision_searches(searches):
    """Répartit jeux, consoles et appareils dans chaque fenêtre de rotation."""
    buckets = {"GAME": [], "CONSOLE": [], "DEVICE": []}
    for search in searches:
        product_type = str(search.get("product_type", "")).upper()
        bucket = product_type if product_type in {"GAME", "CONSOLE"} else "DEVICE"
        buckets[bucket].append(search)
    device_priority = {
        "SMARTPHONE": 0, "TABLET": 1, "COMPUTER": 2, "LAPTOP": 2,
        "DESKTOP": 2, "MINI_PC": 2, "EREADER": 3, "AUDIO": 4,
        "CAMERA": 5, "ACTION_CAMERA": 5, "SMARTWATCH": 6,
        "STREAMING": 7, "ELECTRONICS": 8, "TOOL": 9,
    }

    buckets["DEVICE"].sort(key=lambda search: (
        device_priority.get(str(search.get("product_type", "")).upper(), 20),
        -precision_demand_score(search), norm(search.get("query", "")),
    ))
    # Huit recherches précises donnent quatre jeux, deux consoles et deux
    # appareils tant que chaque famille contient encore des références.
    pattern = ("GAME", "GAME", "DEVICE", "CONSOLE",
               "GAME", "DEVICE", "CONSOLE", "GAME")
    ordered = []
    positions = {key: 0 for key in buckets}
    total = sum(len(rows) for rows in buckets.values())
    while len(ordered) < total:
        added = False
        for key in pattern:
            index = positions[key]
            if index < len(buckets[key]):
                ordered.append(buckets[key][index])
                positions[key] += 1
                added = True
        if not added:
            break
    return ordered


def precision_demand_score(search):
    """Lit la demande d'une recherche locale ou d'un appareil externe."""
    rules = search.get("rules", [])
    rule = rules[0] if rules else {}
    return int(search.get("demand_score", rule.get("demand_score", 0)) or 0)


def game_platform_excluded(rule, cfg):
    excluded = {norm(value) for value in cfg.get("excluded_game_platforms", [])}
    platforms = {norm(value) for value in rule.get("platform_any", [])}
    return bool(excluded & platforms)


def _search_history_score(search, history):
    row = history.get(norm(search.get("query", "")), {}) if isinstance(history, dict) else {}
    alerts = float(row.get("alerts", 0) or 0)
    candidates = float(row.get("candidates", 0) or 0)
    runs = float(row.get("runs", 0) or 0)
    empty_streak = float(row.get("empty_streak", 0) or 0)
    # Une alerte vaut beaucoup plus qu'un simple candidat. Les recherches
    # jamais essayées restent devant celles qui sont durablement vides.
    if runs <= 0:
        return 1.0
    return alerts * 20.0 + candidates * 3.0 - min(empty_streak, 8.0) * 2.0


def _priority_rotation(pool, count, cursor, history):
    if not pool or count <= 0:
        return []
    ranked = sorted(
        pool, key=lambda row: _search_history_score(row, history), reverse=True,
    )
    proven = [row for row in ranked if _search_history_score(row, history) > 1.0]
    priority_count = min(len(proven), max(1, count // 3))
    selected = proven[:priority_count]
    rotating = [row for row in ranked if row not in selected]
    if rotating:
        start = cursor % len(rotating)
        for offset in range(len(rotating)):
            row = rotating[(start + offset) % len(rotating)]
            if row not in selected:
                selected.append(row)
            if len(selected) >= count:
                break
    return selected[:count]


def update_search_performance(previous, cycle_metrics, retention_runs=200):
    history = previous if isinstance(previous, dict) else {}
    for query_key, current in cycle_metrics.items():
        old = history.get(query_key, {})
        candidates = int(current.get("candidates", 0) or 0)
        alerts = int(current.get("alerts", 0) or 0)
        history[query_key] = {
            "query": current.get("query", query_key),
            "runs": min(int(old.get("runs", 0) or 0) + 1, retention_runs),
            "received": min(int(old.get("received", 0) or 0)
                            + int(current.get("received", 0) or 0), 1_000_000),
            "candidates": min(int(old.get("candidates", 0) or 0)
                              + candidates, 100_000),
            "alerts": min(int(old.get("alerts", 0) or 0) + alerts, 100_000),
            "empty_streak": 0 if candidates else min(
                int(old.get("empty_streak", 0) or 0) + 1, 50,
            ),
        }
    return history


def adaptive_api_budget(cfg, rate_state):
    base_requests = int(cfg.get("api_budget_max_requests_per_cycle", 20))
    base_units = float(cfg.get("api_budget_max_units_per_cycle", base_requests))
    level = max(0, min(int((rate_state or {}).get("penalty_level", 0) or 0), 3))
    factor = 0.75 ** level
    return max(10, int(base_requests * factor)), max(10.0, base_units * factor)


def select_searches_for_run(searches, cfg, cursor=None, persist_cursor=True,
                            search_history=None):
    """Planifie un mélange stable de recherches larges et de produits précis.

    Les recherches larges découvrent les titres inattendus. Les recherches
    précises garantissent que le catalogue contrôlé n'est pas seulement utilisé
    après le téléchargement : ses noms de produits sont réellement recherchés
    sur Vinted. Elles ne lisent qu'une page et coûtent donc deux fois moins
    d'appels qu'une recherche large en mode snipe.
    """
    searches = _collapse_personal_variants(_merge_searches(searches))
    limit = max(1, int(cfg.get("max_searches_per_run", 10)))
    anchors = {norm(x) for x in cfg.get("always_search_queries", [])}
    fixed = [search for search in searches if norm(search.get("query", "")) in anchors]
    fixed = fixed[:limit]
    remaining = [search for search in searches if search not in fixed]
    precision = [
        search for search in remaining
        if search.get("_search_pool") == "precision"
    ]
    discovery = [
        search for search in remaining
        if search.get("_search_pool") != "precision"
    ]
    slots = max(0, limit - len(fixed))
    if cursor is None:
        cursor_data = load_json(SCAN_CURSOR_PATH, {})
        try:
            cursor = (
                int(cursor_data.get("cursor", 0))
                if (isinstance(cursor_data, dict)
                    and cursor_data.get("schema") in {4, 5})
                else 0
            )
        except (TypeError, ValueError):
            cursor = 0
    else:
        try:
            cursor = int(cursor)
        except (TypeError, ValueError):
            cursor = 0
    precision_limit = max(0, int(cfg.get("precision_searches_per_run", 0)))
    precision_slots = min(slots, precision_limit, len(precision))
    discovery_slots = min(slots - precision_slots, len(discovery))

    def rotate(pool, count):
        if not pool or count <= 0:
            return []
        start = cursor % len(pool)
        return [pool[(start + index) % len(pool)] for index in range(count)]

    history = search_history or {}
    rotating_precision = _priority_rotation(
        precision, precision_slots, cursor, history,
    )
    rotating_discovery = _priority_rotation(
        discovery, discovery_slots, cursor, history,
    )

    # Si un pool ne remplit pas son quota, l'autre récupère les créneaux.
    missing = slots - len(rotating_precision) - len(rotating_discovery)
    if missing > 0 and len(precision) > len(rotating_precision):
        extra_pool = [row for row in precision if row not in rotating_precision]
        rotating_precision.extend(rotate(extra_pool, min(missing, len(extra_pool))))
        missing = slots - len(rotating_precision) - len(rotating_discovery)
    if missing > 0 and len(discovery) > len(rotating_discovery):
        extra_pool = [row for row in discovery if row not in rotating_discovery]
        rotating_discovery.extend(rotate(extra_pool, min(missing, len(extra_pool))))

    rotating_count = max(
        len(rotating_precision), len(rotating_discovery), 1 if remaining else 0,
    )
    if remaining and slots:
        # Le curseur suit en priorité le pool précis. Utiliser la taille du
        # mélange total pouvait laisser quelques produits définitivement hors
        # des fenêtres lorsque les deux tailles avaient un diviseur commun.
        rotation_modulus = len(precision) or len(discovery) or len(remaining)
        cursor = (cursor + rotating_count) % max(rotation_modulus, 1)
        if persist_cursor:
            save_json(SCAN_CURSOR_PATH, {"schema": 5, "cursor": cursor})
    cfg["_search_plan_counts"] = {
        "fixed": len(fixed),
        "precision": len(rotating_precision),
        "discovery": len(rotating_discovery),
    }
    cfg["_next_scan_cursor"] = cursor
    return fixed + rotating_precision + rotating_discovery

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
        + ({"JACKPOT": 35, "SUPER_JACKPOT": 75}.get(row.get("price_zone"), 0))
        + min(float(row.get("price_drop_pct") or 0), 50) * 2
        - float(row.get("photo_condition_penalty") or 0)
        - age * 0.50
        - views * 0.05
        - favourites * 0.50
    )


def select_diverse_candidates(candidates, max_total, max_per_category):
    """Conserve les meilleurs scores sans laisser une famille monopoliser les alertes."""
    selected = []
    counts = {}
    total_limit = max(1, int(max_total))
    family_limit = max(1, int(max_per_category))
    for row in candidates:
        family = str(row.get("category") or row.get("product_type") or "AUTRE")
        if counts.get(family, 0) >= family_limit:
            continue
        selected.append(row)
        counts[family] = counts.get(family, 0) + 1
        if len(selected) >= total_limit:
            break
    return selected

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

def candidate_price_zone(rule, product_type, price, cfg):
    """Donne une priorité spéciale aux vrais jeux Switch très bon marché.

    Cette règle intervient uniquement après la classification stricte. Une
    housse, une boîte ou une console ne peut donc jamais gagner ce bonus.
    """
    platforms = " ".join(str(value) for value in rule.get("platform_any", []))
    is_switch_game = (
        product_type == "GAME"
        and "SWITCH" in _platform_families_in(platforms)
    )
    if is_switch_game:
        if float(price) <= float(cfg.get("switch_game_super_jackpot_eur", 5)):
            return "SUPER_JACKPOT"
        if float(price) <= float(cfg.get("switch_game_jackpot_eur", 10)):
            return "JACKPOT"
    hot_buy_price = float(rule.get("hot_buy_price", 0) or 0)
    return "JACKPOT" if hot_buy_price and float(price) <= hot_buy_price else "CIBLE"

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
    zone = {
        "JACKPOT": " JACKPOT",
        "SUPER_JACKPOT": " SUPER JACKPOT ≤5€",
    }.get(row.get("price_zone"), "")
    type_label = {
        "CONSOLE": "CONSOLE", "GAME": "JEU", "ACCESSORY": "ACCESSOIRE",
        "SMARTPHONE": "SMARTPHONE", "TABLET": "TABLETTE",
        "LAPTOP": "ORDINATEUR", "DESKTOP": "ORDINATEUR",
        "COMPUTER": "ORDINATEUR", "TOOL": "OUTIL",
    }.get(str(row.get("product_type", "")).upper(), "ARTICLE")
    reference = f"Référence {row.get('model', '?')}"
    if row.get("catalog_description"):
        reference += f" — {row['catalog_description']}"
    if row.get("reference_source"):
        reference += f" — source {row['reference_source']}"
    body = (f"[{type_label}] [{row['opportunity_score']}/10]{zone} {row['title']} | "
            f"{reference} | "
            f"Achat {row['listing_price']:.2f} EUR | revente "
            f"{row['resale_low']:.0f}-{row['resale_high']:.0f} EUR | "
            f"bénéfice prudent {row['margin_low']:.2f} EUR | {row['reason']}"
            f"{risk_suffix}")
    if row.get("price_drop_pct"):
        body = (
            f"🔥 BAISSE -{float(row['price_drop_pct']):.0f}% | "
            f"ancien prix {float(row['previous_price']):.2f} EUR | " + body
        )
    headers = {
        "Title": f"Vinted {type_label} {row['opportunity_score']}/10",
        "Priority": (
            "max" if row.get("price_zone") == "SUPER_JACKPOT"
            else "high" if row["opportunity_score"] >= 8 else "default"
        ),
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
OFF_PLATFORM_PAYMENT_FALLBACK_TERMS = frozenset((
    "pas de paiement vinted", "pas de payement vinted",
    "paiement vinted refusé", "paiement vinted refuse",
    "payement vinted refusé", "payement vinted refuse",
    "no vinted payment", "vinted payment not accepted",
    "do not pay via vinted", "dont pay via vinted",
    "paiement hors vinted", "payement hors vinted",
    "paiement en dehors de vinted", "payment outside vinted",
    "paypal uniquement", "paypal seulement", "paypal only",
    "paiement par paypal", "payement par paypal", "payment by paypal",
    "paypal accepté", "paypal accepte", "paypal accepted",
    "paypal entre proches", "paypal amis et famille",
    "paypal friends and family", "paypal f&f",
    "virement bancaire", "versement bancaire", "bank transfer",
    "banküberweisung", "bankuberweisung", "überweisung", "uberweisung",
    "bonifico bancario", "transferencia bancaria",
    "overschrijving", "bankoverschrijving",
    "paiement par revolut", "payement par revolut", "revolut only",
    "paiement par wise", "payement par wise", "wise only",
    "western union", "paiement en crypto", "payment in crypto",
    "paiement bitcoin", "payment bitcoin", "bizum uniquement",
    "paiement par bizum", "cash app only", "cashapp only",
))

PAYPAL_REJECTION_MARKERS = frozenset((
    "pas de paypal", "paypal refusé", "paypal refuse",
    "paypal non accepté", "paypal non accepte", "no paypal",
    "paypal not accepted",
))

BANK_TRANSFER_REJECTION_MARKERS = frozenset((
    "pas de virement", "virement refusé", "virement refuse",
    "aucun virement", "sans virement",
    "no bank transfer", "bank transfer not accepted",
    "keine überweisung", "keine uberweisung",
))


def catalog_item_text(item):
    """Texte de sécurité disponible sans appeler l'endpoint détail."""
    values = []
    for key in ("title", "description", "subtitle", "content", "status_message"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return " ".join(dict.fromkeys(values))


def off_platform_payment_hits(text, blacklist):
    """Repère un paiement imposé hors Vinted sans bloquer son refus explicite."""
    configured = blacklist.get("off_platform_payment_blacklist", ())
    terms = tuple(configured) if configured else OFF_PLATFORM_PAYMENT_FALLBACK_TERMS
    hits = matching_terms(text, terms)
    if not hits:
        return []
    paypal_refused = any(term_present(text, marker)
                         for marker in PAYPAL_REJECTION_MARKERS)
    transfer_refused = any(term_present(text, marker)
                           for marker in BANK_TRANSFER_REJECTION_MARKERS)
    safe_hits = []
    for hit in hits:
        hit_n = norm(hit)
        if paypal_refused and "paypal" in hit_n:
            continue
        if transfer_refused and any(word in hit_n for word in (
                "virement", "versement", "bank transfer", "uberweisung",
                "bonifico", "transferencia", "overschrijving")):
            continue
        safe_hits.append(hit)
    return safe_hits


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
    payment_hits = off_platform_payment_hits(combined, blacklist)
    if payment_hits:
        return True, "off_platform_payment_blacklist", payment_hits[:3], []
    risks = matching_terms(combined, blacklist.get("suspicious_words", []))
    return False, "", [], risks[:3]

EMPTY_PACKAGING_TERMS = frozenset((
    "boite vide", "boîte vide", "boitier vide", "boîtier vide",
    "empty box", "box only", "boite seule", "boîte seule",
    "juste la boite", "juste la boîte", "sans console", "sans jeu",
    "game case only", "coffret vide", "empty collector box",
    "verpackung leer", "leere verpackung", "nur verpackung",
    "solo scatola", "scatola vuota", "solo caja", "caja vacia",
    "caja vacía", "lege doos", "alleen doos", "caixa vazia", "só caixa",
))

PACKAGING_START_TERMS = frozenset((
    "boite", "boîte", "boitier", "boîtier", "coffret", "box", "case",
    "verpackung", "scatola", "caja", "doos", "caixa", "embalagem",
))

PRODUCT_INCLUDED_MARKERS = frozenset((
    "avec console", "console incluse", "console inclus", "console included",
    "avec le jeu", "avec jeu", "jeu inclus", "jeu incluse", "game included",
    "contient la console", "contient le jeu", "complet avec console",
))

CONSOLE_ACCESSORY_TERMS = frozenset((
    "manette", "manettes", "controller", "controllers", "mando", "mandos",
    "controler", "controlers", "comando", "comandos", "gâchette", "gachette",
    "trigger", "triggers",
    "gamepad", "gamepads", "joy-con", "joycon", "joystick", "joysticks",
    "dock", "chargeur", "charger", "carregador", "cargador", "caricatore",
    "ladegerät", "ladegerat", "oplader", "câble", "cable", "coque",
    "housse", "étui", "etui", "pochette", "sac", "sacoche", "transport bag",
    "carry bag", "carrying case", "borsa", "bolsa", "carcasa", "cover",
    "covers", "protection", "protective cover",
    "protective case", "shell", "skin", "sleeve", "funda", "custodia",
    "capa", "hoes", "hoesje", "beschermhoes", "schutzhülle",
    "schutzhulle", "support", "stand", "batterie", "battery", "batterij",
    "accu", "akku", "batería", "bateria", "batteria", "écran", "ecran",
    "joystick", "lecteur seul", "carte mémoire", "carte memoire",
    "micro sd", "microsd", "adaptateur", "adapter", "ac adapter",
    "power adapter", "adaptador", "adattatore", "lector nfc", "lecteur nfc",
    "nfc reader", "alimentation", "stylet", "stylets", "stylus", "ar card",
    "carte ar", "thumb grip", "thumb grips", "button cap", "button caps",
    "grip cap", "keychain", "key chain", "porte-clés", "porte cles",
    "portachiavi", "schlüsselanhänger", "schlusselanhanger",
))

GAME_ACCESSORY_TERMS = frozenset((
    "steelbook", "poster", "figurine", "amiibo", "goodies", "artbook",
    "guide", "manuel seul", "manual only", "porte-clés", "porte cles",
    "keychain", "key chain", "portachiavi", "schlüsselanhänger",
    "schlusselanhanger", "code seul", "download code only", "jaquette seule",
    "cover only",
))

GAME_HARDWARE_TERMS = frozenset((
    "console", "pack console", "switch oled", "switch lite",
    "ps5 slim", "ps5 pro", "ps4 pro", "xbox series s", "xbox series x",
    "xbox one x", "3ds xl", "2ds xl", "game boy advance sp",
))

ELECTRONICS_ACCESSORY_TERMS = frozenset((
    "chargeur", "charger", "câble", "cable", "coque", "housse", "étui",
    "etui", "batterie", "battery", "objectif", "lens", "écran", "ecran",
    "adaptateur", "power adapter", "boitier vide", "boîtier vide",
    "télécommande", "telecommande", "remote", "bracelet", "watch band",
    "wristband", "strap", "station de charge", "charging dock",
    "verre trempé", "verre trempe", "screen protector", "protection écran",
    "protection ecran", "vitre de protection", "film de protection",
    "film hydrogel", "protecteur écran", "protecteur ecran",
    "tempered glass", "screen guard", "privacy glass", "hydrogel film",
    "camera lens protector", "phone case", "iphone case", "silicone case",
    "clear case", "bumper case", "cristal templado", "vidrio templado",
    "protector de pantalla", "vetro temperato", "pellicola protettiva",
    "handyhülle", "handyhulle", "panzerglas", "displayschutz",
    "schutzglas", "telefoonhoesje", "screenprotector", "beschermglas",
    "película de vidro", "pelicula de vidro", "vidro temperado",
    "protetor de tela", "szkło hartowane", "szklo hartowane",
    "folia ochronna", "funda", "carcasa", "custodia", "capa", "hoesje",
    "schutzhülle", "schutzhulle", "skin", "shell", "case only", "courroie",
    "sacoche", "pochette", "support mural", "wall mount", "trépied",
    "trepied", "tripod", "micro sd", "carte mémoire", "carte memoire",
    "memory card", "ear pads", "coussinets", "pièce détachée",
    "piece detachee", "replacement part",
))

ELECTRONICS_INCLUDED_MARKERS = frozenset((
    "avec chargeur", "chargeur inclus", "with charger", "+ chargeur",
    "avec câble", "avec cable", "câble inclus", "cable included", "+ cable",
    "avec batterie", "batterie incluse", "with battery", "+ batterie",
    "avec objectif", "objectif inclus", "with lens", "+ objectif", "+ lens",
    "avec télécommande", "avec telecommande", "remote included", "+ remote",
    "+ télécommande", "+ telecommande", "avec bracelet", "bracelet inclus",
    "avec étui", "avec etui", "avec housse", "avec coque", "avec pochette",
    "with case", "+ housse", "+ coque", "avec carte mémoire",
    "avec carte memoire",
))

PHONE_ACCESSORY_OR_PART_TERMS = frozenset((
    "coque", "housse", "etui", "étui", "case", "cover", "funda",
    "fundas", "capa", "capas", "carcasa", "custodia", "hoes", "hoesje",
    "telefoonhoesje", "beschermhoes",
    "handyhülle", "handyhulle", "schutzhülle", "schutzhulle",
    "verre trempé", "verre trempe", "tempered glass", "screen protector",
    "protecteur écran", "protecteur ecran", "protection écran",
    "protection ecran", "film hydrogel", "vitre de protection",
    "cristal templado", "vidrio templado", "vetro temperato",
    "panzerglas", "beschermglas", "screenprotector", "displayschutz",
    "pellicola protettiva", "película de vidro", "pelicula de vidro",
    "vidro temperado", "protetor de tela", "szkło hartowane",
    "szklo hartowane", "folia ochronna",
    "écran lcd", "ecran lcd", "lcd screen", "lcd display", "display lcd",
    "replacement screen", "écran de remplacement", "ecran de remplacement",
    "scherm", "display", "digitizer", "vitre arrière", "vitre arriere",
    "back glass", "châssis", "chassis", "nappe", "flex cable",
    "batterie", "battery", "caméra arrière", "camera module",
    "pièce détachée", "piece detachee", "replacement part",
))

PHONE_COMPLETE_SIGNALS = frozenset((
    "téléphone", "telephone", "smartphone", "mobile phone", "gsm",
    "fonctionne bien", "fonctionne parfaitement", "parfaitement fonctionnel",
    "bon état", "bon etat", "très bon état", "tres bon etat",
    "comme neuf", "état neuf", "etat neuf", "fully working", "works well",
    "good condition", "excellent condition", "in goede staat",
    "werkt goed", "funciona bien", "funciona perfectamente",
    "buone condizioni", "funziona bene", "desbloqueado", "unlocked",
    "désimlocké", "desimlocke", "double sim", "dual sim",
))

PHONE_STORAGE_RE = re.compile(
    r"\b(?:16|32|64|128|256|512)\s*(?:go|gb)\b|\b1\s*tb\b", re.I,
)

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
    "stylet", "stylets", "stylus", "thumb grip", "thumb grips",
    "button cap", "button caps", "grip cap", "ar card", "carte ar",
))

ACCESSORY_ONLY_MARKERS = frozenset((
    "pour", "para", "for", "voor", "per", "fur", "für", "compatible",
    "seul", "seule", "only", "remplacement", "replacement", "de rechange",
    "ersatz", "sostituzione", "repuesto",
))

PROTECTIVE_COVER_TERMS = frozenset((
    "coque", "housse", "étui", "etui", "pochette", "cover", "covers",
    "protection", "protective cover", "protective case", "case", "shell",
    "skin", "sleeve", "funda", "custodia", "capa", "hoes", "hoesje",
    "beschermhoes", "schutzhülle", "schutzhulle", "sac", "sacoche",
    "transport bag", "carry bag", "carrying case", "borsa", "bolsa",
    "carcasa",
))

ACCESSORY_INCLUDED_MARKERS = frozenset((
    "avec manette", "manette incluse", "manette inclus", "avec controller",
    "controller included", "with controller", "avec housse", "avec coque",
    "avec étui", "avec etui", "avec pochette", "with case", "case included",
    "avec cover", "cover included", "avec support", "avec dock", "dock inclus",
    "avec chargeur", "chargeur inclus", "+ manette", "+ coque", "+ housse",
))

BATTERY_TERMS = frozenset((
    "batterie", "battery", "batterij", "accu", "akku",
    "batería", "bateria", "batteria",
))

BATTERY_INCLUDED_MARKERS = frozenset((
    "avec batterie", "batterie incluse", "batterie incluse",
    "with battery", "battery included", "met batterij",
    "batterij inbegrepen", "con batería", "con bateria",
    "batteria inclusa", "mit akku",
))

KNOWN_GAME_TITLE_TERMS = frozenset((
    "little nightmares", "minecraft", "fortnite", "fifa", "ea sports fc",
    "call of duty", "grand theft auto", "gta", "zelda", "mario kart",
    "super mario", "pokemon", "pokémon", "animal crossing", "splatoon",
    "metroid", "xenoblade", "elden ring", "spider-man", "spider man",
    "god of war", "gran turismo", "horizon", "last of us", "returnal",
    "stellar blade", "silent hill", "ratchet", "final fantasy",
    "switch sports", "mario party", "top gun", "michael jackson",
    "inazuma eleven", "yo-kai", "yokai", "dragon ball", "pilotwings",
    "mind quiz", "sonic boom", "nintendo tennis", "nes tennis",
    "house of ashes", "trials rising", "trial rising", "assassin's creed",
    "assassins creed", "hogwarts", "notruf 112", "tekken",
    "ben 10 omniverse", "mario tennis", "mario & luigi", "mario and luigi",
    "superstar saga", "ring fit", "rampage world tour", "smash football",
    "arkham origins", "starfox",
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
    "GAMECUBE": frozenset(("gamecube", "nintendo gamecube")),
    "3DS": frozenset(("3ds", "nintendo 3ds")),
    "DS": frozenset(("nintendo ds", "ds lite")),
    "PSP": frozenset(("psp", "psp 3000", "psp 3004")),
    "VITA": frozenset(("ps vita", "psvita", "playstation vita")),
    "N64": frozenset(("n64", "nintendo 64")),
    "SNES": frozenset(("snes", "super nintendo")),
    "NES": frozenset(("nes", "nintendo nes")),
    "GBA": frozenset(("gba", "game boy advance", "gameboy advance")),
    "GAME_BOY": frozenset(("game boy", "gameboy", "gbc")),
}

CONSOLE_INTENT_TERMS = frozenset((
    "console", "consola", "konsole", "pack console", "bundle console",
    "pack ps5", "bundle ps5", "pack switch", "bundle switch",
    "pack xbox", "bundle xbox",
))

# Mots pouvant précéder normalement le modèle dans une annonce de console.
# Tout autre préfixe (« Digimon Survive Nintendo Switch », « Rampage N64 »)
# indique généralement que la plateforme sert à décrire un jeu.
CONSOLE_LISTING_PREFIX_WORDS = frozenset((
    "a", "à", "vendre", "vend", "vends", "vente", "vendo", "selling",
    "sell", "verkaufe", "vendo", "mon", "ma", "mes", "my", "mia",
    "mio", "belle", "beau", "superbe", "magnifique", "neuf", "neuve",
    "nouveau", "nouvelle", "occasion", "lot", "pack", "bundle",
    "original", "officiel", "officielle", "sony", "microsoft", "nintendo",
    "playstation", "xbox", "new",
))

# Descripteurs matériels admis après le nom de la console. Un autre mot après
# « Nintendo Switch », « PS Vita », « Nintendo 64 », etc. est généralement le
# titre d'un jeu : « Nintendo Switch Ring Fit » ne doit pas devenir une console.
CONSOLE_LISTING_SUFFIX_WORDS = frozenset((
    "console", "consola", "konsole", "fat", "slim", "pro", "digital",
    "disc", "disque", "lecteur", "oled", "lcd", "lite", "v1", "v2",
    "series", "one", "x", "s", "new", "xl", "2ds", "3ds", "3000",
    "3004", "go", "gbc", "gba", "sp", "nes", "snes", "gamecube",
    "noir", "noire", "black", "blanc", "blanche", "white", "bleu",
    "blue", "rouge", "red", "gris", "grise", "grey", "gray", "neon",
    "néon", "rose", "pink", "violet", "purple", "jaune", "yellow",
    "vert", "green", "edition", "édition", "speciale", "spéciale",
    "collector", "limitee", "limitée", "standard", "originale",
    "original", "fonctionnelle", "fonctionnel", "testee", "testée",
    "marche", "parfait", "parfaite", "etat", "état", "bon", "bonne",
    "comme", "neuf", "neuve", "occasion", "complete", "complète",
    "seule", "seul", "avec", "sans", "plus", "lot", "pack", "bundle",
    "manette", "manettes", "controller", "controllers", "joycon",
    "joy", "con", "dock", "chargeur", "cable", "câble", "hdmi",
    "alimentation", "batterie", "battery", "batterij", "akku", "bateria",
    "batteria", "incluse", "inclus", "included", "neuve", "neuf",
    "boite", "boîte", "carton", "garantie", "facture",
    "stockage", "ssd", "hdd", "to", "tb", "go", "gb", "512", "500",
    "256", "128", "64", "32", "1", "2", "jailbreak", "moddee",
    "moddée", "craquee", "craquée",
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


def _console_platform_preceded_by_product_name(title, rule):
    """Repère un nom de jeu placé avant une plateforme de console.

    Les vendeurs écrivent très souvent « nom du jeu - Nintendo Switch ».
    Une règle matérielle générique ne doit pas transformer cette structure en
    console. Les mots de vente, marques et nombres restent admis afin de garder
    « Vends ma Nintendo Switch » et « Lot 2 Nintendo Switch ».
    """
    title_n = norm(title)
    identity = " ".join((
        str(rule.get("brand", "")), str(rule.get("model", "")),
        " ".join(str(value) for value in rule.get("must_contain", [])),
    ))
    families = _platform_families_in(identity)
    aliases = sorted(
        {
            norm(alias)
            for family in families
            for alias in PLATFORM_FAMILIES.get(family, ())
            if norm(alias)
        },
        key=len, reverse=True,
    )
    occurrences = []
    for alias in aliases:
        match = re.search(rf"(?:^|\s){re.escape(alias)}(?:\s|$)", title_n)
        if match:
            start = match.start()
            if start and title_n[start].isspace():
                start += 1
            occurrences.append((start, -len(alias), alias))
    if not occurrences:
        return False

    start, _, _ = min(occurrences)
    prefix = title_n[:start].strip()
    if not prefix:
        return False
    prefix_words = set(prefix.split())
    identity_words = set(norm(identity).split())
    allowed = CONSOLE_LISTING_PREFIX_WORDS | identity_words
    return any(
        not word.isdigit() and word not in allowed
        for word in prefix_words
    )


def _console_platform_followed_by_product_name(title, rule):
    """Repère un titre de jeu placé après le nom de la plateforme."""
    title_n = norm(title)
    identity = " ".join((
        str(rule.get("brand", "")), str(rule.get("model", "")),
        " ".join(str(value) for value in rule.get("must_contain", [])),
    ))
    families = _platform_families_in(identity)
    aliases = sorted(
        {
            norm(alias)
            for family in families
            for alias in PLATFORM_FAMILIES.get(family, ())
            if norm(alias)
        },
        key=len, reverse=True,
    )
    for alias in aliases:
        match = re.search(rf"(?:^|\s){re.escape(alias)}(?:\s|$)", title_n)
        if not match:
            continue
        suffix = title_n[match.end():].strip()
        if not suffix:
            continue
        words = suffix.split()
        # « édition Zelda/Mario » décrit souvent une console en édition
        # spéciale ; on la garde, tout en laissant un avertissement souple.
        if any(word in {"edition", "édition", "speciale", "spéciale"} for word in words):
            return False
        if any(
            not word.isdigit() and word not in CONSOLE_LISTING_SUFFIX_WORDS
            for word in words
        ):
            return True
    return False


def _starts_with_term(title, terms):
    title_n = norm(title)
    return any(
        title_n == norm(term) or title_n.startswith(norm(term) + " ")
        for term in terms
    )


def _packaging_only_title(title):
    """Détecte « Boîte Minecraft/PS5 » sans bloquer « avec le jeu/console »."""
    if not _starts_with_term(title, PACKAGING_START_TERMS):
        return False
    return not any(
        term_present(title, marker) for marker in PRODUCT_INCLUDED_MARKERS
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
        "ACTION_CAMERA": "ACTION_CAMERA", "CAMERA_ACTION": "ACTION_CAMERA",
        "LISEUSE": "EREADER", "EREADER": "EREADER",
        "STREAMING": "STREAMING", "BOITIER_STREAMING": "STREAMING",
        "SMARTWATCH": "SMARTWATCH", "MONTRE_CONNECTEE": "SMARTWATCH",
        "DRAWING_TABLET": "DRAWING_TABLET", "TABLETTE_GRAPHIQUE": "DRAWING_TABLET",
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

def strict_product_type_check(source_search, rule, title, cfg=None, item_text=""):
    """Bloque seulement les incompatibilités certaines entre produit et titre."""
    if cfg is not None and not cfg.get("strict_product_type", True):
        return True, ""
    if any(term_present(title, word) for word in EMPTY_PACKAGING_TERMS):
        return False, "emballage vide"
    if _packaging_only_title(title):
        return False, "annonce centrée sur la boîte, produit non confirmé"

    product_type = infer_product_type(source_search, rule)
    combined_text = f"{title} {item_text}".strip()
    # Vérifier le titre brut avant les exceptions de modèle ou d'état.
    # Une coque « en très bon état » ne prouve pas la présence du téléphone.
    if any(term_present(title, word) for word in (
        "kinder minecraft", "kinder - minecraft", "leeg", "doosje",
    )):
        return False, "produit dérivé ou emballage seul"
    mobile_part_terms = (
        "coque", "coques", "case", "cases", "hülle", "hüllen",
        "hulle", "hullen", "handyhüllen", "handyhullen", "fundas",
        "funda", "capas", "capa", "pantalla", "salva schermo",
        "folio touch", "gamesir g8",
    )
    if product_type in {"SMARTPHONE", "TABLET", "ELECTRONICS"}:
        hits = [word for word in mobile_part_terms if term_present(title, word)]
        included = any(term_present(title, marker) for marker in (
            "avec coque", "avec sa coque", "with case", "+ coque",
            "avec housse", "avec étui", "avec etui",
        ))
        if hits and (not included or _starts_with_term(title, mobile_part_terms)
                     or term_present(title, "lot de")):
            return False, "protection ou périphérique vendu séparément"
    if product_type == "CONSOLE":
        has_console_word = any(term_present(title, word) for word in (
            "console", "consola", "konsole",
        ))
        if (not has_console_word and
                _console_platform_preceded_by_product_name(title, rule)):
            return False, "nom de produit avant la plateforme: probablement un jeu"
        if (not has_console_word and
                _console_platform_followed_by_product_name(title, rule)):
            return False, "nom de produit après la plateforme: probablement un jeu"
        game_words = (
            "jeu", "jeux", "game", "games", "juego", "juegos",
            "gioco", "giochi", "spiel", "spiele", "spel", "spelletjes",
            "jogo", "jogos", "cartouche", "cartuccia", "cartridge",
        )
        if any(term_present(title, word) for word in game_words) and not has_console_word:
            return False, "jeu, pas console"
        if (not has_console_word and any(
                term_present(title, game_title)
                for game_title in KNOWN_GAME_TITLE_TERMS)):
            return False, "titre de jeu connu, pas console"
        accessory_hit = any(term_present(title, word) for word in CONSOLE_ACCESSORY_TERMS)
        protective_cover_hit = any(
            term_present(title, word) for word in PROTECTIVE_COVER_TERMS
        )
        battery_hit = any(term_present(title, word) for word in BATTERY_TERMS)
        battery_included = any(
            term_present(title, marker) for marker in BATTERY_INCLUDED_MARKERS
        )
        accessory_included = battery_included or any(
            term_present(title, marker) for marker in ACCESSORY_INCLUDED_MARKERS
        )
        if battery_hit and not has_console_word and not battery_included:
            return False, "batterie seule, pas console"
        accessory_only = (
            _starts_with_term(title, CONSOLE_ACCESSORY_TERMS)
            or any(term_present(title, word) for word in ACCESSORY_ONLY_MARKERS)
            or (accessory_hit and not has_console_word and not accessory_included)
        )
        if protective_cover_hit and not accessory_included:
            return False, "protection seule, pas console"
        # « Manette PS5 pour console » reste un accessoire même si le mot
        # console figure dans le titre. « Console PS5 avec manette » est admis.
        if accessory_hit and accessory_only:
            return False, "accessoire, pas console"
    elif product_type == "GAME":
        protective_cover_hit = any(
            term_present(title, word) for word in PROTECTIVE_COVER_TERMS
        )
        protective_cover_included = any(
            term_present(title, marker) for marker in ACCESSORY_INCLUDED_MARKERS
        )
        if protective_cover_hit and not protective_cover_included:
            return False, "protection ou pochette, pas jeu"
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
        protective_cover_hit = any(
            term_present(title, word) for word in PROTECTIVE_COVER_TERMS
        )
        protective_cover_included = any(
            term_present(title, marker) for marker in ACCESSORY_INCLUDED_MARKERS
        )
        if subtype == "CONTROLLER" and protective_cover_hit and not protective_cover_included:
            return False, "protection, pas manette"
        if subtype == "CONTROLLER" and any(
                term_present(title, word) for word in ("dock", "chargeur", "station de charge")):
            return False, "chargeur, pas manette"
        if subtype == "DOCK" and any(
                term_present(title, word) for word in ("câble", "cable", "chargeur", "adaptateur")):
            return False, "câble, pas dock"
        if subtype == "WHEEL" and any(
                term_present(title, word) for word in ("jeu", "game", "support volant")):
            return False, "jeu ou support, pas volant"
    elif product_type == "SMARTPHONE":
        part_hit = any(
            term_present(combined_text, word)
            for word in PHONE_ACCESSORY_OR_PART_TERMS
        )
        part_first = _starts_with_term(title, PHONE_ACCESSORY_OR_PART_TERMS)
        complete_signal = (
            any(term_present(combined_text, marker)
                for marker in PHONE_COMPLETE_SIGNALS)
            or bool(PHONE_STORAGE_RE.search(combined_text))
        )
        phone_included = any(term_present(combined_text, marker) for marker in (
            "téléphone avec", "telephone avec", "smartphone avec",
            "iphone avec", "galaxy avec", "pixel avec", "phone with",
            "vendu avec coque", "fourni avec coque", "avec sa coque",
        ))
        if part_hit and (part_first or not (complete_signal or phone_included)):
            return False, "accessoire ou pièce de téléphone"
    else:
        accessory_hit = any(
            term_present(title, word) for word in ELECTRONICS_ACCESSORY_TERMS
        )
        included = any(
            term_present(title, marker) for marker in ELECTRONICS_INCLUDED_MARKERS
        )
        accessory_only = (
            _starts_with_term(title, ELECTRONICS_ACCESSORY_TERMS)
            or any(term_present(title, marker) for marker in ACCESSORY_ONLY_MARKERS)
        )
        if accessory_hit and (accessory_only or not included):
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

def rule_match_confidence(rule, title, text, deep=False):
    """Correspondance exacte ou souple, toujours ancrée sur le modèle.

    Les références électroniques peuvent définir des groupes de synonymes.
    Elles ne doivent pas atteindre 100 %, mais un identifiant distinctif reste
    obligatoire. Les codes alphanumériques ne sont jamais corrigés comme des
    fautes de frappe, ce qui empêche A1842 de devenir A1843 ou XM4 de devenir XM5.
    """
    title_n = norm(title)
    full = norm(f"{title} {text}")
    must = frozenset(rule.get("must_contain", []))
    any_kw = frozenset(rule.get("any_contain", []))
    exclude = frozenset(rule.get("exclude", []))
    excluded_hits = [
        word for word in exclude if term_present_normalized(full, word)
    ]
    if excluded_hits:
        product_type = str(rule.get("product_type", "")).upper()
        electronic_type = product_type not in {"CONSOLE", "GAME", "ACCESSORY"}
        included_accessory = electronic_type and any(
            term_present_normalized(full, marker)
            for marker in ELECTRONICS_INCLUDED_MARKERS
        )
        accessory_norms = {norm(word) for word in ELECTRONICS_ACCESSORY_TERMS}
        blocking_hits = [
            word for word in excluded_hits
            if not included_accessory or norm(word) not in accessory_norms
        ]
        if blocking_hits:
            return False, 0.0

    identity = tuple(rule.get("identity_any", []))
    groups = tuple(rule.get("match_groups", []))
    if identity or groups:
        if identity and not any(
                term_present_normalized(title_n, alias) for alias in identity):
            return False, 0.0
        normalised_groups = []
        for group in groups:
            aliases = group if isinstance(group, (list, tuple)) else [group]
            aliases = tuple(alias for alias in aliases if norm(alias))
            if aliases:
                normalised_groups.append(aliases)
        matched = sum(
            any(term_present_normalized(title_n, alias) for alias in aliases)
            for aliases in normalised_groups
        )
        total = len(normalised_groups)
        confidence = matched / total if total else 1.0
        threshold = min(0.95, max(0.50, float(rule.get("match_threshold", 0.75))))
        minimum = int(rule.get("min_match_groups", 2) or 2)
        if total and (matched < min(minimum, total) or confidence < threshold):
            return False, confidence
    else:
        if must and not all(term_present_normalized(title_n, word) for word in must):
            return False, 0.0
        if any_kw and not any(term_present_normalized(title_n, word) for word in any_kw):
            return False, 0.0
        confidence = 1.0 if must or any_kw else 0.5

    if not deep:
        return True, confidence
    platform = rule.get("platform_any", [])
    hardware = rule.get("hardware_any", [])
    if platform and not any(term_present_normalized(full, word) for word in platform):
        return False, confidence
    if hardware:
        hardware_text = title_n if rule.get("hardware_in_title") else full
        if not any(term_present_normalized(hardware_text, word) for word in hardware):
            return False, confidence
    return True, confidence


def rule_match(rule, title, text, deep=False):
    return rule_match_confidence(rule, title, text, deep)[0]

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
    cfg = cfg or {}
    vocabulary = cfg.get("_fuzzy_vocabulary")
    if not vocabulary:
        vocabulary = build_rule_vocabulary(rule_index)
    classification_title = canonicalize_product_title(str(title), vocabulary)
    title_matches = []
    for source_search, rule in rule_index:
        matched, confidence = rule_match_confidence(
            rule, classification_title, classification_title, deep=False,
        )
        if not matched:
            continue
        type_ok, _ = strict_product_type_check(
            source_search, rule, classification_title, cfg,
        )
        if not type_ok:
            continue
        title_matches.append((
            infer_product_type(source_search, rule), source_search, rule, confidence,
        ))
    if not title_matches:
        return None, None

    # La classification se fait avant le filtre de prix. Ainsi un jeu PS5 trop
    # cher n'est jamais recyclé en fausse « console PS5 à 20 EUR ».
    has_console_intent = any(
        term_present(classification_title, term) for term in CONSOLE_INTENT_TERMS
    )
    matched_types = {item[0] for item in title_matches}
    if "GAME" in matched_types and not has_console_intent:
        title_matches = [item for item in title_matches if item[0] == "GAME"]
    elif "CONSOLE" in matched_types and has_console_intent:
        title_matches = [item for item in title_matches if item[0] == "CONSOLE"]

    matches = []
    for product_type, source_search, rule, confidence in title_matches:
        price_limit = source_search.get("price_to")
        resale_low = rule.get("resale_low")
        ratio = float(rule.get("max_buy_ratio", 0.50))
        ratio_limit = float(resale_low) * ratio if resale_low is not None else None
        effective_limit = price_limit if price_limit is not None else ratio_limit
        if effective_limit is not None and float(price) > float(effective_limit):
            continue
        priority = (
            round(float(confidence), 4),
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
                        per_page=50, page=1, stats=None, api_budget=None):
    request_started = time.perf_counter()
    url = f"{base_url}/api/v2/catalog/items"
    params = {
        "search_text": query,
        "order": "newest_first",
        "per_page": max(1, min(int(per_page), 50)),
        "page": max(1, int(page)),
    }
    if price_to is not None:
        params["price_to"] = float(price_to)
    if api_budget is not None:
        try:
            await api_budget.spend(1.0, "catalog")
        except ApiBudgetExceeded as exc:
            if stats is not None:
                stats["catalog_budget_blocked"] += 1
            LOGGER.warning("%s", exc)
            return []
    await limiter.wait("catalog", cost=1.0)
    if stats is not None:
        stats["catalog_requested"] += 1
    try:
        async with session.get(url, params=params, headers=headers, timeout=10) as resp:
            await limiter.register_response(resp.status, resp.headers, "catalog")
            if resp.status == 429 and stats is not None:
                stats["http_429"] = stats.get("http_429", 0) + 1
            if resp.status != 200:
                LOGGER.warning(
                    "Catalogue HTTP %s pour %s page %s",
                    resp.status, query, page,
                )
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
                      rule_index=None, price_history=None, api_budget=None,
                      reference_catalog=None, device_catalog=None,
                      cycle_claimed_ids=None, claimed_lock=None,
                      search_metrics=None):
    query = search["query"]
    name = search.get("name", query)
    LOGGER.info(f"\n[API-TEST] {name} → {query}")

    max_items = min(
        int(search.get("max_items", cfg.get("max_items_per_search", 50))),
        int(cfg.get("max_items_per_search", 50)),
    )
    per_page = int(cfg.get("catalog_per_page", 50))
    configured_pages = search.get("max_pages", cfg.get("snipe_max_pages", 2))
    max_pages = (
        max(1, min(int(configured_pages), 4))
        if cfg.get("snipe_mode", True) else 1
    )
    items = []
    known_ids = set()
    for page in range(1, max_pages + 1):
        page_items = await catalog_items(
            query, search.get("price_to"), base_url, limiter, session,
            headers, per_page=per_page, page=page, stats=stats,
            api_budget=api_budget,
        )
        if not page_items:
            break
        for item in page_items:
            item_id = str(item.get("id"))
            if item_id not in known_ids:
                known_ids.add(item_id)
                items.append(item)
        if len(page_items) < per_page:
            break
        last_age = listing_age_hours(catalog_timestamp(page_items[-1]))
        if (last_age is not None and
                last_age > float(cfg.get("max_listing_age_hours", 0.5))):
            LOGGER.info(
                "SNIPE arrêt page %s | dernier article %.1f min",
                page, last_age * 60,
            )
            break
    if cycle_claimed_ids is not None:
        unique_items = []
        if claimed_lock is None:
            claimed_lock = asyncio.Lock()
        async with claimed_lock:
            for item in items:
                item_id = str(item.get("id"))
                if item_id in cycle_claimed_ids:
                    stats["duplicates_skipped_early"] = stats.get(
                        "duplicates_skipped_early", 0,
                    ) + 1
                    continue
                cycle_claimed_ids.add(item_id)
                unique_items.append(item)
        items = unique_items

    if not items:
        if search_metrics is not None:
            search_metrics[norm(query)] = {
                "query": query, "received": 0, "examined": 0,
                "candidates": 0, "alerts": 0,
            }
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

        previous_price, drop_pct, is_price_drop = price_drop_event(
            price_history if price_history is not None else {},
            item_id, price, cfg, title,
        )

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
        price_drop_max_age = float(cfg.get("price_drop_max_age_hours", 168))
        if (age > float(cfg.get("max_listing_age_hours", 0.5)) and
                not (is_price_drop and age <= price_drop_max_age)):
            stats["rejected_old"] += 1
            LOGGER.debug("  X Âge | %.1fh", age)
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        # 2. Vérifier vu (sauf si baisse de prix, simplifié ici)
        if item_already_seen(seen_ids, search, item_id) and not is_price_drop:
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
        item_text = catalog_item_text(item)
        blocked, blocked_group, blocked_hits, _ = blacklist_check(
            title, item_text, blacklist, check_accessories=False,
        )
        if blocked:
            if blocked_group == "off_platform_payment_blacklist":
                stats["rejected_unsafe_payment"] = (
                    stats.get("rejected_unsafe_payment", 0) + 1
                )
                LOGGER.info(
                    "  X Paiement hors Vinted | %s | %s",
                    ", ".join(blocked_hits), title[:60],
                )
            else:
                stats["rejected_blacklist"] += 1
                LOGGER.debug("  X Blacklist | %s", title[:60])
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        # 5. Trouver la règle
        # Une correspondance PriceCharting exige le titre exact ET la
        # plateforme. Elle est évaluée avant les règles générales : un jeu
        # « Nintendo 64 Rampage » ne peut donc plus hériter du prix N64.
        matched_search, matched_rule = (None, None)
        if reference_catalog is not None:
            matched_search, matched_rule = reference_catalog.match(title, price)
            if matched_rule is not None:
                type_ok, _ = strict_product_type_check(
                    matched_search, matched_rule, title, cfg,
                    item_text=item_text,
                )
                if not type_ok:
                    matched_search, matched_rule = (None, None)
        if matched_rule is None and device_catalog is not None:
            matched_search, matched_rule = device_catalog.match(title, price)
            if matched_rule is not None:
                type_ok, _ = strict_product_type_check(
                    matched_search, matched_rule, title, cfg,
                    item_text=item_text,
                )
                if not type_ok:
                    matched_search, matched_rule = (None, None)
        if matched_rule is None and rule_index is not None:
            matched_search, matched_rule = choose_known_product(
                rule_index, title, price, cfg,
            )
        elif matched_rule is None:
            # Les recherches spécifiques profitent du même Levenshtein que
            # le mode découverte ; aucun second classificateur divergent.
            local_index = [(search, rule) for rule in search.get("rules", [])]
            matched_search, matched_rule = choose_known_product(
                local_index, title, price, cfg,
            )
        if matched_rule is None or matched_search is None:
            stats["rejected_rule"] += 1
            continue

        type_ok, type_reason = strict_product_type_check(
            matched_search, matched_rule, title, cfg, item_text=item_text,
        )
        if not type_ok:
            stats["rejected_rule"] += 1
            LOGGER.debug("  X Type | %s | %s", type_reason, title[:60])
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue

        category = matched_search.get("category", "")
        product_type = infer_product_type(matched_search, matched_rule)
        if product_type == "GAME" and game_platform_excluded(matched_rule, cfg):
            stats["rejected_rule"] += 1
            mark_seen(seen_ids, search_seen_key(search, item_id), seen_meta)
            continue
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
        price_zone = candidate_price_zone(matched_rule, product_type, price, cfg)
        if price_zone in {"JACKPOT", "SUPER_JACKPOT"}:
            score = min(10, score + 1)
        score -= min(
            float(cfg.get("soft_risk_penalty_cap", 1.0)),
            len(filter_risks) * float(cfg.get("soft_risk_penalty", 0.35)),
        )
        score = round(max(1.0, score), 1)
        if score < float(cfg.get("min_candidate_score", 5)):
            stats["rejected_score"] += 1
            continue

        # 9. Alerte
        size = "?"
        published_dt = parse_vinted_timestamp(created)
        published_at = published_dt.isoformat(timespec="seconds") if published_dt else ""

        risk_parts = list(filter_risks)
        if is_price_drop:
            risk_parts.append(
                f"baisse de prix {previous_price:.2f} → {price:.2f} EUR (-{drop_pct:.0f}%)"
            )
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
            "catalog_description": matched_rule.get("description", ""),
            "reference_source": matched_rule.get("external_source", "catalogue contrôlé"),
            "sales_volume": matched_rule.get("sales_volume", ""),
            "match_confidence": round(
                rule_match_confidence(
                    matched_rule,
                    canonicalize_product_title(
                        title, cfg.get("_fuzzy_vocabulary", ()),
                    ),
                    title,
                )[1] * 100,
            ),
            "size": size,
            "opportunity_score": score,
            "title": title,
            "published_at": published_at,
            "age_minutes": int(round(age * 60)) if age is not None else "",
            "favourite_count": fav_count if fav_count is not None else "",
            "view_count": view_count if view_count is not None else "",
            "seller_type": "pro" if seller.get("is_business") else "particulier",
            "previous_price": previous_price if previous_price is not None else "",
            "price_drop_pct": drop_pct if is_price_drop else "",
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
            "_source_query": query,
        }

        alerts.append(row)
        stats["candidates"] += 1
        LOGGER.info(
            "  + CANDIDAT %s/10 | %s | %s | %.2f EUR | marge +%.2f EUR",
            score, freshness_label(age, cfg), title[:58], price, margin_low,
        )

    if search_metrics is not None:
        search_metrics[norm(query)] = {
            "query": query,
            "received": len(items),
            "examined": min(len(items), max_items),
            "candidates": len(alerts),
            "alerts": 0,
        }
    return alerts

# ---------- Main ----------
async def main_async():
    cycle_started = time.perf_counter()
    cfg = load_json(CONFIG_PATH, {})
    if not cfg:
        LOGGER.error("config.json introuvable")
        return

    blacklist = load_json(BLACKLIST_PATH, {})
    rejection_terms = apply_confirmed_rejections(
        CONFIRMED_REJECTIONS_PATH, blacklist,
    )
    if rejection_terms:
        LOGGER.info("Rejets confirmés ajoutés à la blacklist: %s", rejection_terms)
    target_searches = load_target_products()
    reference_catalog = ReferenceCatalog.load(PRICECHARTING_CATALOG_PATH)
    device_catalog = DeviceCatalog.load(DEVICE_CATALOG_PATH)
    lowered_phones = device_catalog.apply_buy_price_multiplier(
        "SMARTPHONE", cfg.get("smartphone_buy_price_multiplier", 1.0),
    )
    legacy_searches = list(cfg.get("searches", []))
    use_legacy = bool(cfg.get("use_legacy_search_rules", False))
    cfg["searches"] = target_searches + (legacy_searches if use_legacy else [])
    LOGGER.info("Profil marché: %s produits cibles chargés", len(target_searches))
    if reference_catalog:
        LOGGER.info(
            "PriceCharting: %s références rapides indexées localement",
            len(reference_catalog),
        )
    else:
        LOGGER.info("PriceCharting: cache absent, catalogue contrôlé utilisé")
    if device_catalog:
        LOGGER.info(
            "Appareils et outils: %s références indexées localement",
            len(device_catalog),
        )
        if lowered_phones:
            LOGGER.info(
                "Smartphones: %s seuils d'achat abaissés à %.0f%%",
                lowered_phones,
                float(cfg.get("smartphone_buy_price_multiplier", 1.0)) * 100,
            )
    else:
        LOGGER.info("Appareils et outils: cache absent, catalogue manuel utilisé")
    if legacy_searches and not use_legacy:
        LOGGER.info(
            "%s anciennes recherches ignorées pour éviter les faux positifs",
            len(legacy_searches),
        )
    personal_count = apply_personal_filters(cfg, blacklist)
    LOGGER.info("%s recherches personnelles ajoutées", personal_count)
    product_searches = list(cfg.get("searches", []))
    sold_provider = SoldListingsProvider(
        SOLD_LISTINGS_PATH,
        cfg.get("sold_listings_max_age_days", 30),
        cfg.get("sold_listings_min_samples", 3),
    )
    sold_enriched = sold_provider.enrich(product_searches)
    if sold_enriched:
        LOGGER.info("Données de ventes réalisées | %s produits enrichis", sold_enriched)
    cache_fingerprint = search_fingerprint(product_searches, {
        "min_demand_score": cfg.get("min_demand_score", 4),
        "strict_product_type": cfg.get("strict_product_type", True),
        "classifier_schema": CLASSIFIER_SCHEMA,
    })
    rule_index = load_rule_index(SEARCH_CACHE_PATH, cache_fingerprint)
    if rule_index is None:
        rule_index = build_rule_index(product_searches, cfg)
        save_rule_index(SEARCH_CACHE_PATH, cache_fingerprint, rule_index)
        cache_status = "reconstruit"
    else:
        cache_status = "réutilisé"
    classifier_terms = (
        CONSOLE_ACCESSORY_TERMS | GAME_ACCESSORY_TERMS |
        PROTECTIVE_COVER_TERMS | KNOWN_GAME_TITLE_TERMS |
        EMPTY_PACKAGING_TERMS
    )
    cfg["_fuzzy_vocabulary"] = build_rule_vocabulary(
        rule_index, classifier_terms,
    )
    LOGGER.info(
        "Profil demande chargé | %s produits reconnus | cache %s | "
        "vocabulaire Levenshtein %s termes",
        len(rule_index), cache_status,
        vocabulary_size(cfg["_fuzzy_vocabulary"]),
    )

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

    raw_prices = load_json(PRICE_HISTORY_PATH, {})
    price_history = raw_prices if isinstance(raw_prices, dict) else {}

    prune_seen_state(seen_ids, seen_meta, cfg.get("seen_retention_days", 30))
    prune_price_history(
        price_history, cfg.get("price_history_retention_days", 30),
    )

    limiter = TokenBucketRateLimiter(
        capacity=cfg.get("token_bucket_capacity", 3),
        refill_per_second=cfg.get("token_bucket_refill_per_second", 1.0),
        min_jitter=cfg.get("request_delay_min_seconds", 0.15),
        max_jitter=cfg.get("request_delay_max_seconds", 0.50),
        max_backoff=cfg.get("backoff_max_seconds", 60.0),
    )
    rate_state = load_json(RATE_STATE_PATH, {})
    budget_requests, budget_units = adaptive_api_budget(cfg, rate_state)
    api_budget = ApiCostController(budget_requests, budget_units)
    if int((rate_state or {}).get("penalty_level", 0) or 0):
        LOGGER.warning(
            "Budget API adaptatif | niveau %s | %s requêtes maximum",
            rate_state.get("penalty_level"), budget_requests,
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
        "rejected_score", "rejected_unsafe_payment", "catalog_budget_blocked",
        "photo_analysed", "photo_failed", "photo_flagged", "http_429",
        "duplicates_skipped_early",
    )}

    concurrency = max(1, int(cfg.get("api_max_concurrency", 3)))
    dns_status = await dns_healthcheck(
        base_url, cfg.get("dns_healthcheck_timeout_seconds", 3),
    )
    LOGGER.info(
        "Santé DNS | %s | %.1f ms | %s",
        dns_status["host"], dns_status["latency_ms"],
        ", ".join(dns_status["addresses"][:2]),
    )
    async with managed_http_session(base_url, cfg, headers) as session:

        discovery_mode = bool(cfg.get("discovery_mode", True))
        if discovery_mode:
            broad_searches = (
                list(cfg.get("discovery_searches", []))
                + reference_catalog.discovery_searches(
                    cfg.get("pricecharting_discovery_max_price", 300),
                )
                + device_catalog.discovery_searches(
                    cfg.get("device_catalog_discovery_max_price", 800),
                )
            )
            excluded_game_queries = {
                norm(value) for value in cfg.get("excluded_game_platforms", [])
            }
            broad_searches = [
                search for search in broad_searches
                if not (
                    str(search.get("name", "")).startswith("PRICECHARTING - ")
                    and norm(search.get("query", "")) in excluded_game_queries
                )
            ]
            broad_searches = [
                {**search, "_search_pool": "discovery"}
                for search in broad_searches
            ]
            # La voie précise parcourt toutes les références contrôlées à tour
            # de rôle. Une seule page suffit car elle est triée par fraîcheur.
            precision_sources = (
                product_searches + device_catalog.precision_searches()
            )
            precision_min_demand = int(
                cfg.get("precision_min_demand_score", 0),
            )
            precision_sources = [
                search for search in precision_sources
                if precision_demand_score(search) >= precision_min_demand
            ]
            precision_searches = [
                {
                    **search,
                    "_search_pool": "precision",
                    "max_pages": 1,
                    "max_items": min(
                        int(search.get("max_items", cfg.get("catalog_per_page", 50))),
                        int(cfg.get("catalog_per_page", 50)),
                    ),
                }
                for search in interleave_precision_searches(precision_sources)
            ]
            all_searches = broad_searches + precision_searches
        else:
            all_searches = product_searches
        search_history = load_json(SEARCH_PERFORMANCE_PATH, {})
        searches = select_searches_for_run(
            all_searches, cfg, cursor=scan_cursor, persist_cursor=False,
            search_history=search_history,
        )
        max_catalog_items = int(cfg.get("max_catalog_items_per_run", 50))
        per_search = max(1, max_catalog_items // max(1, len(searches)))
        cfg["max_items_per_search"] = min(
            int(cfg.get("max_items_per_search", 5)), per_search,
        )
        plan = cfg.get("_search_plan_counts", {})
        planned_requests = sum(
            max(1, min(int(search.get(
                "max_pages", cfg.get("snipe_max_pages", 2),
            )), 4))
            for search in searches
        )
        planned_items = sum(
            min(
                int(search.get("max_items", cfg["max_items_per_search"])),
                int(cfg.get("catalog_per_page", 50)) * max(
                    1, min(int(search.get(
                        "max_pages", cfg.get("snipe_max_pages", 2),
                    )), 4),
                ),
            )
            for search in searches
        )
        LOGGER.info(
            "Cycle double voie | %s/%s recherches | jusqu'à %s annonces",
            len(searches), len(_collapse_personal_variants(_merge_searches(all_searches))),
            min(max_catalog_items, planned_items),
        )
        LOGGER.info(
            "Plan recherches | fixes %s | produits précis %s | découverte %s | "
            "budget prévu <= %s requêtes",
            plan.get("fixed", 0), plan.get("precision", 0),
            plan.get("discovery", 0), planned_requests,
        )
        semaphore = asyncio.Semaphore(concurrency)
        cycle_claimed_ids = set()
        claimed_lock = asyncio.Lock()
        search_metrics = {}

        async def bounded_scan(search):
            async with semaphore:
                search_started = time.perf_counter()
                try:
                    rows = await scan_search(
                        search, cfg, blacklist, seen_ids, seen_meta,
                        limiter, session, base_url, headers, stats,
                        rule_index=rule_index if discovery_mode else None,
                        price_history=price_history,
                        api_budget=api_budget,
                        reference_catalog=reference_catalog,
                        device_catalog=device_catalog,
                        cycle_claimed_ids=cycle_claimed_ids,
                        claimed_lock=claimed_lock,
                        search_metrics=search_metrics,
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
        for search, res in zip(searches, results):
            if isinstance(res, Exception):
                stats["searches_failed"] += 1
                LOGGER.error(
                    "Erreur recherche %s: %s",
                    search.get("query", "?"), res,
                )
            else:
                candidates.extend(res)

        # Une même annonce peut apparaître dans plusieurs recherches. On ne
        # conserve que sa meilleure évaluation avant de calculer le classement.
        best_by_item = {}
        for row in candidates:
            item_id = str(row.get("item_id"))
            previous = best_by_item.get(item_id)
            if previous is None or candidate_rank(row) > candidate_rank(previous):
                best_by_item[item_id] = row
        candidates = sorted(
            best_by_item.values(), key=candidate_rank, reverse=True,
        )

        photo_summary = {
            "analysed": 0, "failed": 0, "flagged": 0, "model_used": False,
        }
        if cfg.get("photo_analysis_enabled", True) and candidates:
            configured_model = Path(cfg.get(
                "photo_condition_model_path", "models/condition_classifier.onnx",
            ))
            if not configured_model.is_absolute():
                configured_model = ROOT / configured_model
            analyzer = PhotoConditionAnalyzer(
                model_path=configured_model,
                labels=cfg.get("photo_condition_labels", ()),
                confidence_threshold=cfg.get("photo_condition_min_confidence", 0.70),
                min_resolution=cfg.get("photo_min_resolution", 320),
                blur_threshold=cfg.get("photo_blur_threshold", 4.0),
                max_download_bytes=cfg.get("photo_max_download_bytes", 5_000_000),
            )
            photo_summary = await enrich_rows_with_photos(
                candidates, session, analyzer,
                max_images=cfg.get("photo_analysis_max_candidates", 6),
                concurrency=cfg.get("photo_analysis_concurrency", 2),
                timeout_seconds=cfg.get("photo_analysis_timeout_seconds", 5),
            )
            stats["photo_analysed"] += photo_summary["analysed"]
            stats["photo_failed"] += photo_summary["failed"]
            stats["photo_flagged"] += photo_summary["flagged"]
            candidates.sort(key=candidate_rank, reverse=True)
            LOGGER.info(
                "Photos | %s analysées | %s signalées | %s échecs | ONNX %s",
                photo_summary["analysed"], photo_summary["flagged"],
                photo_summary["failed"],
                "actif" if photo_summary["model_used"] else "absent (heuristiques)",
            )
        selected = select_diverse_candidates(
            candidates,
            cfg.get("max_alerts_per_run", 5),
            cfg.get("max_alerts_per_category", 4),
        )
        selected_ids = {str(row.get("item_id")) for row in selected}
        for row in selected:
            query_key = norm(row.get("_source_query", ""))
            if query_key and query_key in search_metrics:
                search_metrics[query_key]["alerts"] += 1

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
            price_history,
        )
        search_history = update_search_performance(search_history, search_metrics)
        save_json(SEARCH_PERFORMANCE_PATH, search_history)
        previous_penalty = max(0, min(int((rate_state or {}).get(
            "penalty_level", 0,
        ) or 0), 3))
        next_penalty = min(3, previous_penalty + 1) if stats["http_429"] else max(
            0, previous_penalty - 1,
        )
        save_json(RATE_STATE_PATH, {
            "schema": 1,
            "penalty_level": next_penalty,
            "last_429_count": stats["http_429"],
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
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
            "score %s | filtres durs %s | paiement hors Vinted %s | pro %s",
            stats["rejected_old"], stats["rejected_seen"],
            stats["rejected_rule"], stats["rejected_profit"],
            stats["rejected_score"], stats["rejected_blacklist"],
            stats["rejected_unsafe_payment"], stats["rejected_pro"],
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
        budget_status = api_budget.snapshot()
        LOGGER.info(
            "Budget API | %s/%s requêtes | %.1f/%.1f unités | %s bloquée(s)",
            budget_status["requests"], budget_status["max_requests"],
            budget_status["units"], budget_status["max_units"],
            budget_status["blocked"],
        )

        requested = stats["catalog_requested"]
        if requested and stats["catalog_success"] / requested < 0.5:
            raise RuntimeError("Moins de 50% des catalogues ont répondu: scan invalide")
        if requested and stats["catalog_success"] and stats["catalog_items"] == 0:
            raise RuntimeError("Catalogues vides: scan probablement bloqué par Vinted")
        sampled_ages = stats["age_known"] + stats["age_unknown"]
        if sampled_ages >= 3 and stats["age_known"] == 0:
            raise RuntimeError("Aucun âge lisible dans le catalogue: scan invalide")

        top_opportunities = [{
            "item_id": row.get("item_id"),
            "title": row.get("title"),
            "url": row.get("url"),
            "product_type": row.get("product_type"),
            "listing_price": row.get("listing_price"),
            "margin_low": row.get("margin_low"),
            "price_drop_pct": row.get("price_drop_pct", ""),
            "photo_condition": row.get("photo_condition", "unknown"),
            "photo_risk": row.get("photo_risk", ""),
            "rank_score": round(candidate_rank(row), 2),
        } for row in selected]
        return {
            "stats": dict(stats),
            "top_opportunities": top_opportunities,
            "search_metrics": search_metrics,
            "dns": dns_status,
            "api_budget": budget_status,
            "duration_seconds": round(cycle_seconds, 3),
        }

async def run_workflow_session():
    """Répète plusieurs cycles courts pendant un même workflow GitHub."""
    cfg = load_json(CONFIG_PATH, {})
    # Une session GitHub reste bornée pour garantir la sauvegarde des données,
    # puis le cron la relance. La limite haute permet une couverture quasi
    # continue sans créer une boucle infinie impossible à terminer proprement.
    cycles = max(1, min(int(cfg.get("cycles_per_workflow", 1)), 12))
    pause_seconds = max(15.0, float(cfg.get("seconds_between_cycles", 75)))
    alert_limit = max(1, int(cfg.get("max_alerts_per_run", 5)))
    LOGGER.info(
        "Session prolongée | %s cycle(s) | pause %.0fs | jusqu'à %s alertes/cycle",
        cycles, pause_seconds, alert_limit,
    )

    cycle_results = []
    for cycle_number in range(1, cycles + 1):
        LOGGER.info("Démarrage cycle %s/%s", cycle_number, cycles)
        result = await main_async()
        if isinstance(result, dict):
            cycle_results.append(result)
        if cycle_number < cycles:
            LOGGER.info("Prochain cycle dans %.0f secondes", pause_seconds)
            await asyncio.sleep(pause_seconds)

    previous_report = load_json(REPORT_PATH, {})
    report = build_workflow_report(
        cycle_results, previous_report,
        history_days=cfg.get("report_history_days", 30),
    )
    save_json(REPORT_PATH, report)
    LOGGER.info(
        "Rapport workflow | %s annonces | %s notifications | %.1f articles/s | %s",
        report["summary"]["listings_examined"],
        report["summary"]["notifications_sent"],
        report["performance"]["items_per_second"], REPORT_PATH,
    )
    regression = report["performance"]["throughput_regression_pct"]
    if regression >= float(cfg.get("performance_regression_warning_pct", 30)):
        LOGGER.warning(
            "PERF régression | débit inférieur de %.1f%% à la moyenne 30 jours",
            regression,
        )

if __name__ == "__main__":
    try:
        run_async(run_workflow_session)
    except KeyboardInterrupt:
        LOGGER.info("Arrêt demandé")

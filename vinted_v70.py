#!/usr/bin/env python3
# VERSION : VINTED_V80_FLASH_FIRST_CLEAN
#
# V8 propre :
# - aucune règle précise obligatoire pour voir une annonce
# - scan newest_first en priorité
# - tri ultra-rapide sur les cartes avant toute ouverture détaillée
# - suivi 30 min des annonces prometteuses
# - apprentissage des ventes rapides confirmées
# - apprentissage titre + description + prix + photo principale
# - favoris/âge lus si Vinted les expose, sans inventer l'information
# - boîtes/accessoires/faibles valeurs restent filtrés
# - ntfy immédiat avant l'analyse lourde

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
import os
import re
import statistics
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

try:
    from PIL import Image, ImageOps
except Exception:
    Image = None
    ImageOps = None

ROOT = Path(__file__).resolve().parent

CONFIG_PATH = ROOT / "config.json"
BLACKLIST_PATH = ROOT / "blacklist.json"
FILTRES_PATH = ROOT / "filtres.json"
EXEMPLES_PATH = ROOT / "exemples.txt"
REJETS_PATH = ROOT / "rejets.txt"

SEEN_PATH = ROOT / "annonces_vues.json"
BASE_PATH = ROOT / "base_apprentissage.json"
ALERTS_PATH = ROOT / "alertes.csv"
HISTORY_PATH = ROOT / "historique_annonces.jsonl"

ITEM_RE = re.compile(r"/items/(\d+)")
MONEY_RE = re.compile(r"(?<!\d)(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:€|EUR)\b", re.I)

SOLD_WORDS = (
    "vendu", "vendue", "sold", "verkocht", "vendido", "vendida",
    "venduto", "venduta", "verkauft", "sprzedane", "sprzedany",
)
RESERVED_WORDS = (
    "réservé", "reserve", "reserved", "gereserveerd", "reserviert",
)

EMPTY_PACKAGING_WORDS = (
    "boite vide", "boîte vide", "empty box", "box only", "case only",
    "caja vacia", "caja vacía", "solo caja", "scatola vuota",
    "custodia vuota", "doosje", "lege doos", "verpakking",
)
ACCESSORY_WORDS = (
    "manette", "controller", "joycon", "joy-con", "dock", "station",
    "chargeur", "charger", "cable", "câble", "adaptateur", "adapter",
    "support", "stand", "soporte", "houder", "pochette", "housse",
    "case", "custodia", "coque", "shell", "écran", "screen", "batterie",
    "battery", "stylet", "stylus", "volant", "wheel", "hub", "vr",
)
MERCH_WORDS = (
    "figurine", "goodie", "poster", "affiche", "pin", "medallion",
    "medaille", "médaille", "porte cle", "porte-clé", "keychain",
    "steelbook", "plv", "display", "artbook", "coin", "piece collector",
)
LOW_VALUE_SPORT_WORDS = (
    "fifa", "pes ", "efootball", "nba 2k", "madden", "nhl ",
    "just dance", "singstar", "fitness", "karaoke",
)
MULTICART_WORDS = (
    "r4", "flashcard", "multicart", "multi cart", "multijeux",
    "multigame", "multijuegos",
)

PLATFORM_CONFLICTS = {
    "JEU_SWITCH": ("ps5", "ps4", "ps3", "ps2", "xbox", "3ds", "2ds", "wii", "vita", "psp"),
    "JEU_PS5": ("ps4", "ps3", "ps2", "xbox", "switch", "3ds", "vita", "psp"),
    "JEU_3DS": ("switch", "ps5", "ps4", "xbox", "vita", "psp"),
    "JEU_VITA": ("switch", "ps5", "ps4", "xbox", "3ds", "psp"),
}

CATEGORY_DEFAULT_CAP = {
    "JEU_SWITCH": 18.0,
    "JEU_PS5": 28.0,
    "JEU_3DS": 22.0,
    "JEU_DS": 15.0,
    "JEU_VITA": 22.0,
    "JEU_PSP": 15.0,
    "JEU_RETRO": 25.0,
    "ELECTRONIQUE": 25.0,
    "CONSOLE": 200.0,
}

CATEGORY_MIN_SCORE = {
    "JEU_SWITCH": 66,
    "JEU_PS5": 66,
    "JEU_3DS": 68,
    "JEU_DS": 70,
    "JEU_VITA": 68,
    "JEU_PSP": 70,
    "JEU_RETRO": 74,
    "ELECTRONIQUE": 72,
    "CONSOLE": 72,
}

MAX_TRACKED = 320
MAX_FAST_RECHECK = 5
FOLLOW_WINDOW_MINUTES = 30
DETAIL_CANDIDATE_LIMIT = 4


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def present(text, term):
    t, q = norm(text), norm(term)
    if not q:
        return False
    if re.fullmatch(r"[a-z0-9]{1,3}", q):
        return re.search(rf"(?<![a-z0-9]){re.escape(q)}(?![a-z0-9])", t) is not None
    return q in t


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_base():
    data = load_json(BASE_PATH, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 8)
    data.setdefault("v8", {})
    v8 = data["v8"]
    v8.setdefault("tracked", {})
    v8.setdefault("fast_sales", [])
    v8.setdefault("fast_disappearances", [])
    v8.setdefault("positive_examples", [])
    v8.setdefault("negative_examples", [])
    v8.setdefault("title_stats", {})
    return data


def save_base(data):
    save_json(BASE_PATH, data)


def load_seen():
    data = load_json(SEEN_PATH, [])
    if isinstance(data, dict):
        data = list(data.keys())
    return {str(x) for x in data}


def save_seen(seen):
    save_json(SEEN_PATH, sorted(seen)[-12000:])


def append_history(event):
    event = dict(event)
    event.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def append_alert(row):
    fields = [
        "timestamp", "category", "search", "brand", "model", "size",
        "opportunity_score", "title", "listing_price", "total_buy_est",
        "resale_low", "resale_high", "margin_low", "margin_high",
        "roi_low", "reason", "url", "item_id"
    ]
    new = not ALERTS_PATH.exists()
    with ALERTS_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


def parse_price(text):
    vals = []
    for m in MONEY_RE.finditer(str(text or "")):
        try:
            v = float(m.group(1).replace(",", "."))
            if 0.25 <= v <= 5000:
                vals.append(v)
        except Exception:
            pass
    return vals[0] if vals else None


def extract_favourites(text):
    raw = norm(text)
    patterns = (
        r"(\d+)\s*(?:favoris|favourites|favorites|favorieten|favoritos|preferiti)\b",
        r"(?:favoris|favourites|favorites|favorieten|favoritos|preferiti)\s*[:\-]?\s*(\d+)",
    )
    for p in patterns:
        m = re.search(p, raw)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    return None


def extract_age_minutes(text):
    """Best effort only. Returns None if Vinted does not expose age text."""
    raw = norm(text)
    m = re.search(r"(?:il y a|vor|ago|hace|fa)\s*(\d+)\s*(?:min|minute|minutes|minuten|minuti)", raw)
    if m:
        return int(m.group(1))
    if re.search(r"(?:a l'instant|à l'instant|just now|gerade eben|appena pubblicato)", raw):
        return 0
    m = re.search(r"(?:il y a|vor|ago|hace|fa)\s*(\d+)\s*(?:h|heure|heures|hour|hours|stunden|ore)", raw)
    if m:
        return int(m.group(1)) * 60
    return None


def image_hash_from_bytes(data):
    if Image is None or ImageOps is None:
        return ""
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        g = ImageOps.grayscale(im).resize((9, 8))
        px = list(g.getdata())
        bits = 0
        for y in range(8):
            row = px[y*9:(y+1)*9]
            for x in range(8):
                bits = (bits << 1) | int(row[x] >= row[x+1])
        return f"{bits:016x}"
    except Exception:
        return ""


def download_hash(url):
    if not url:
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = r.read(4_000_000)
        return image_hash_from_bytes(data)
    except Exception:
        return ""


def hamming_sim(a, b):
    if not a or not b:
        return 0.0
    try:
        x = int(a, 16) ^ int(b, 16)
        d = x.bit_count()
        return 1.0 - d / max(1, len(a) * 4)
    except Exception:
        return 0.0


def title_tokens(text):
    stop = {
        "nintendo","playstation","switch","console","jeu","game","games",
        "edition","version","original","originale","avec","pour","the","of",
        "and","vinted","complet","complete","neuf","neuve","bon","etat",
    }
    return {
        x for x in re.findall(r"[a-z0-9]{3,}", norm(text))
        if x not in stop
    }


def token_similarity(a, b):
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta | tb))


def hard_reject(title, text, category, blacklist):
    t = norm(f"{title} {text}")
    title_n = norm(title)

    # User blacklist terms: only hard groups are applied generically.
    if isinstance(blacklist, dict):
        for key in ("hard_blacklist", "fake_blacklist", "title_accessory_blacklist",
                    "suspicious_words", "low_value_game_blacklist"):
            vals = blacklist.get(key, [])
            if isinstance(vals, list):
                for w in vals:
                    if w and present(title_n if "title" in key else t, w):
                        return True, f"blacklist:{key}:{w}"

    if any(present(title_n, w) for w in EMPTY_PACKAGING_WORDS):
        return True, "emballage seul"

    # Loose cartridge / game without box stays allowed.
    loose_ok = any(present(title_n, x) for x in (
        "cartouche seule", "cartridge only", "jeu sans boite", "jeu sans boîte",
        "disque seul", "disc only", "loose cartridge"
    ))

    if not loose_ok and any(present(title_n, w) for w in ACCESSORY_WORDS):
        # Product-led console bundles are allowed.
        product_led = category == "CONSOLE" and any(
            present(title_n, x) for x in ("console", "ps5", "ps4", "xbox", "switch", "3ds", "2ds", "psp", "vita")
        )
        bundle = any(present(title_n, x) for x in ("avec", "with", "+", "bundle", "pack"))
        if not (product_led and bundle):
            return True, "accessoire"

    if category.startswith("JEU_") and any(present(title_n, w) for w in MERCH_WORDS):
        return True, "produit dérivé"

    if any(present(t, w) for w in LOW_VALUE_SPORT_WORDS):
        return True, "jeu faible valeur"

    if any(present(t, w) for w in MULTICART_WORDS):
        return True, "multicart/r4"

    if re.search(r"\b\d{2,4}\s*(?:jeux|games|juegos|giochi|jogos)\b", t) and \
       any(present(t, x) for x in ("cartouche", "cartridge", "tarjeta", "card", "r4")):
        return True, "multicart numérique"

    conflicts = PLATFORM_CONFLICTS.get(category, ())
    if conflicts:
        expected = {
            "JEU_SWITCH": ("switch",),
            "JEU_PS5": ("ps5", "playstation 5"),
            "JEU_3DS": ("3ds", "2ds"),
            "JEU_VITA": ("vita",),
        }.get(category, ())
        bad = [x for x in conflicts if present(title_n, x)]
        good = [x for x in expected if present(title_n, x)]
        if bad and not good:
            return True, "plateforme contradictoire"

    # Common custom/hack Pokémon GBA titles seen in false positives.
    if category == "JEU_RETRO" and "pokemon" in t:
        suspicious = (
            "neon", "chrome", "cristal de jade", "ambre fire red",
            "feu distorsion", "gaulois", "duo emeraude", "new emeraude",
            "new rubis shadow"
        )
        if any(present(t, x) for x in suspicious):
            return True, "pokemon custom/hack probable"

    return False, ""


def category_price_score(category, price, cap, search_name, title):
    score = 0
    reason = []
    cap = float(cap or CATEGORY_DEFAULT_CAP.get(category, 30.0))

    if category == "JEU_SWITCH":
        if price <= 5: score += 45; reason.append("<=5€")
        elif price <= 10: score += 38; reason.append("<=10€")
        elif price <= 15: score += 30; reason.append("<=15€")
        elif price <= min(18, cap): score += 18
        else: return 0, "prix Switch trop haut"

    elif category == "JEU_PS5":
        if price <= 8: score += 42; reason.append("<=8€")
        elif price <= 15: score += 34; reason.append("<=15€")
        elif price <= 22: score += 24
        elif price <= min(28, cap): score += 14
        else: return 0, "prix PS5 trop haut"

    elif category in {"JEU_3DS","JEU_DS","JEU_VITA","JEU_PSP"}:
        if price <= 8: score += 38
        elif price <= 15: score += 28
        elif price <= cap: score += 15
        else: return 0, "prix portable trop haut"

    elif category == "JEU_RETRO":
        if price <= 8: score += 32
        elif price <= 15: score += 22
        elif price <= cap: score += 10
        else: return 0, "retro trop cher"

    elif category == "ELECTRONIQUE":
        s = norm(f"{search_name} {title}")
        if "ti" in s:
            if price <= 10: score += 40
            else: return 0, "TI >10€"
        else:
            ratio = price / max(cap, 1)
            if ratio <= .35: score += 34
            elif ratio <= .60: score += 22
            elif ratio <= .85: score += 10
            else: return 0, "electronique trop proche plafond"

    elif category == "CONSOLE":
        ratio = price / max(cap, 1)
        if ratio <= .40: score += 38
        elif ratio <= .60: score += 28
        elif ratio <= .80: score += 16
        elif ratio <= 1.0: score += 8
        else: return 0, "console au-dessus plafond"

    else:
        ratio = price / max(cap, 1)
        if ratio <= .50: score += 22
        elif ratio <= .80: score += 10
        else: return 0, "prix peu agressif"

    return score, ",".join(reason)


def learned_bonus(title, price, category, image_hash, base):
    best = 0
    reasons = []
    v8 = base.get("v8", {})

    for ex in v8.get("fast_sales", [])[-180:]:
        if ex.get("category") and category and ex.get("category") != category:
            continue
        ts = token_similarity(title, ex.get("title", ""))
        ps = 0.0
        try:
            ep = float(ex.get("price") or 0)
            if ep > 0:
                ps = max(0.0, 1.0 - abs(price - ep) / max(ep, price, 1))
        except Exception:
            pass
        ims = hamming_sim(image_hash, ex.get("image_hash", "")) if image_hash else 0.0
        combo = ts * .55 + ps * .25 + ims * .20
        if combo >= .68:
            best = max(best, 24)
            reasons = ["très proche vente rapide"]
        elif combo >= .50 and best < 16:
            best = 16
            reasons = ["proche vente rapide"]
        elif combo >= .36 and best < 8:
            best = 8
            reasons = ["profil vendu vite"]

    # User positive examples remain a bonus, never a requirement.
    for ex in v8.get("positive_examples", [])[-120:]:
        if ex.get("category") and category and ex.get("category") != category:
            continue
        ts = token_similarity(title, ex.get("title", ""))
        if ts >= .62:
            best = max(best, 12)
            reasons = ["proche exemple positif"]
        elif ts >= .42:
            best = max(best, 6)

    # Negative learned examples subtract strongly.
    for ex in v8.get("negative_examples", [])[-120:]:
        ts = token_similarity(title, ex.get("title", ""))
        ims = hamming_sim(image_hash, ex.get("image_hash", "")) if image_hash else 0.0
        if max(ts, ims) >= .72:
            return -40, "proche exemple rejeté"

    return best, ",".join(reasons)


def popularity_bonus(favs, fav_delta, age_minutes):
    score = 0
    reasons = []
    if favs is not None:
        if favs >= 20: score += 14; reasons.append(f"{favs} favoris")
        elif favs >= 10: score += 10; reasons.append(f"{favs} favoris")
        elif favs >= 5: score += 6; reasons.append(f"{favs} favoris")
        elif favs >= 2: score += 3
    if fav_delta is not None:
        if fav_delta >= 8: score += 14; reasons.append(f"+{fav_delta} favoris")
        elif fav_delta >= 4: score += 9; reasons.append(f"+{fav_delta} favoris")
        elif fav_delta >= 2: score += 5
    if age_minutes is not None:
        if age_minutes <= 2: score += 12; reasons.append("≤2 min")
        elif age_minutes <= 5: score += 9; reasons.append("≤5 min")
        elif age_minutes <= 10: score += 6
        elif age_minutes <= 20: score += 3
    return score, ",".join(reasons)


def score_card(card, search, blacklist, base, previous_obs):
    title = card.get("title", "")
    text = card.get("text", "")
    category = search.get("category", "")
    price = parse_price(text)
    if price is None:
        return None

    rejected, why = hard_reject(title, text, category, blacklist)
    if rejected:
        return {"score": 0, "price": price, "reject": why}

    cap = search.get("price_to") or CATEGORY_DEFAULT_CAP.get(category, 30)
    pscore, preason = category_price_score(category, price, cap, search.get("name",""), title)
    if pscore <= 0:
        return {"score": 0, "price": price, "reject": preason}

    # Fresh unseen card = high base score. This is the core change.
    score = 44 + pscore
    reasons = ["nouvelle annonce"]
    if preason:
        reasons.append(preason)

    favs = extract_favourites(text)
    age = extract_age_minutes(text)
    prev_favs = None
    if previous_obs:
        try:
            prev_favs = previous_obs.get("favourites")
        except Exception:
            pass
    fav_delta = None
    if favs is not None and prev_favs is not None:
        fav_delta = favs - prev_favs

    pop_score, pop_reason = popularity_bonus(favs, fav_delta, age)
    score += pop_score
    if pop_reason:
        reasons.append(pop_reason)

    # Exact configured rules now only give a small identity bonus.
    title_n = norm(title)
    rule_bonus = 0
    matched_label = ""
    for rule in search.get("rules", []):
        must = rule.get("must_contain", [])
        anyc = rule.get("any_contain", [])
        if must and not all(present(title_n, x) for x in must):
            continue
        if anyc and not any(present(title_n, x) for x in anyc):
            continue
        rule_bonus = max(rule_bonus, 8)
        matched_label = rule.get("label", "")
        break
    score += rule_bonus
    if matched_label:
        reasons.append(f"reconnu:{matched_label[:28]}")

    return {
        "score": int(min(100, score)),
        "price": price,
        "favourites": favs,
        "age_minutes": age,
        "fav_delta": fav_delta,
        "reason": "; ".join(reasons[:5]),
        "matched_label": matched_label,
    }


def ntfy_send(card, search, result, detail=None):
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    endpoint = f"{server}/{urllib.parse.quote(topic, safe='')}"

    score = result["score"]
    price = result["price"]
    title = (detail or {}).get("title") or card.get("title", "")
    url = card.get("url", "")

    if score >= 90:
        label, priority, tags = "BANGER EXPRESS", "urgent", "fire,moneybag"
    elif score >= 78:
        label, priority, tags = "VENTE RAPIDE A VOIR", "high", "zap,shopping_cart"
    else:
        label, priority, tags = "A EVALUER - NOUVEAU", "default", "eyes,shopping_cart"

    favs = result.get("favourites")
    age = result.get("age_minutes")
    body_parts = [
        label,
        title,
        f"Prix: {price:.2f} EUR",
        f"Score: {score}/100",
        result.get("reason", ""),
    ]
    if age is not None:
        body_parts.append(f"Age annonce lu: ~{age} min")
    else:
        body_parts.append("Nouvelle depuis le dernier scan (~5 min max)")
    if favs is not None:
        body_parts.append(f"Favoris lus: {favs}")
    if result.get("fav_delta") is not None:
        body_parts.append(f"Evolution favoris: {result['fav_delta']:+d}")
    body_parts.append(url)
    body = "\n".join(x for x in body_parts if x)

    headers = {
        "Title": f"{label} | {price:.2f} EUR",
        "Priority": priority,
        "Tags": tags,
        "Click": url,
        "Actions": f"view, Ouvrir Vinted, {url}",
    }
    image_url = (detail or {}).get("image_url", "")
    if image_url:
        headers["Attach"] = image_url

    try:
        req = urllib.request.Request(
            endpoint,
            data=body.encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=7) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"  ! ntfy: {e}")
        return False


def title_from_card(c):
    options = [
        c.get("anchor_title",""), c.get("aria_label",""),
        c.get("img_alt",""), c.get("anchor_text","")
    ]
    for s in options:
        s = re.sub(r"\s+", " ", str(s or "")).strip()
        if 3 <= len(s) <= 220 and not MONEY_RE.fullmatch(s):
            return s
    txt = re.sub(r"\s+", " ", c.get("text","")).strip()
    # Drop price-ish tail.
    txt = MONEY_RE.split(txt)[0].strip(" -|")
    return txt[:220]


async def extract_cards(page):
    data = await page.locator('a[href*="/items/"]').evaluate_all(
        """els => els.map(a => {
          let node = a;
          let text = (a.innerText || '').trim();
          for (let i=0; i<5 && node; i++, node=node.parentElement) {
            const t=(node.innerText || '').trim();
            if (t.length>text.length && t.length<1600) text=t;
          }
          const img=a.querySelector('img');
          return {
            href:a.href||'',
            text:text,
            anchor_text:(a.innerText||'').trim(),
            aria_label:a.getAttribute('aria-label')||'',
            anchor_title:a.getAttribute('title')||'',
            img_alt:img ? (img.getAttribute('alt')||'') : '',
            img_src:img ? (img.getAttribute('src')||'') : ''
          };
        })"""
    )
    out, dedupe = [], set()
    for x in data:
        m = ITEM_RE.search(x.get("href",""))
        if not m:
            continue
        iid = m.group(1)
        if iid in dedupe:
            continue
        dedupe.add(iid)
        x["item_id"] = iid
        x["url"] = x.get("href","").split("?")[0]
        x["title"] = title_from_card(x)
        out.append(x)
    return out


async def verify_listing(page, url, fallback_title=""):
    detail = None
    try:
        detail = await page.context.new_page()
        await detail.goto(url, wait_until="domcontentloaded", timeout=8000)
        await detail.wait_for_timeout(250)

        title = fallback_title
        try:
            og = await detail.locator('meta[property="og:title"]').get_attribute("content", timeout=900)
            if og:
                title = re.sub(r"\s*\|\s*Vinted.*$", "", og, flags=re.I).strip()
        except Exception:
            pass

        parts = []
        for sel in ('meta[property="og:description"]', 'meta[name="description"]'):
            try:
                v = await detail.locator(sel).first.get_attribute("content", timeout=800)
                if v: parts.append(v)
            except Exception:
                pass
        try:
            main = await detail.locator("main").inner_text(timeout=1300)
            if main: parts.append(main[:5000])
        except Exception:
            pass
        full = "\n".join(parts)[:7000]

        img = ""
        try:
            img = await detail.locator('meta[property="og:image"]').get_attribute("content", timeout=800) or ""
        except Exception:
            pass

        price = None
        for sel in ('meta[property="product:price:amount"]', 'meta[itemprop="price"]'):
            try:
                v = await detail.locator(sel).first.get_attribute("content", timeout=700)
                if v:
                    m = re.search(r"\d+(?:[.,]\d{1,2})?", v)
                    if m:
                        price = float(m.group(0).replace(",", "."))
                        break
            except Exception:
                pass

        favs = extract_favourites(full)
        age = extract_age_minutes(full)
        sold = any(present(full, w) for w in SOLD_WORDS)
        reserved = any(present(full, w) for w in RESERVED_WORDS)

        return {
            "ok": True, "title": title, "text": full, "image_url": img,
            "price": price, "favourites": favs, "age_minutes": age,
            "sold": sold, "reserved": reserved,
        }
    except Exception as e:
        return {"ok": False, "title": fallback_title, "error": str(e)[:120]}
    finally:
        if detail:
            try:
                await detail.close()
            except Exception:
                pass


def infer_category_from_search(search):
    cat = str(search.get("category","") or "")
    if cat:
        return cat
    nme = norm(f"{search.get('name','')} {search.get('query','')}")
    if "switch" in nme: return "JEU_SWITCH"
    if "ps5" in nme: return "JEU_PS5"
    if "3ds" in nme: return "JEU_3DS"
    if "vita" in nme: return "JEU_VITA"
    if "psp" in nme: return "JEU_PSP"
    return "ARTICLE"


def compact_searches(cfg):
    """Collapse duplicate query/category pairs. Exact rules remain attached only as bonus."""
    merged = {}
    for s in cfg.get("searches", []):
        if not isinstance(s, dict) or not s.get("query"):
            continue
        s = dict(s)
        s["category"] = infer_category_from_search(s)
        key = (norm(s.get("query")), s["category"])
        if key not in merged:
            merged[key] = s
        else:
            old = merged[key]
            old["rules"] = list(old.get("rules", [])) + list(s.get("rules", []))
            caps = [x for x in (old.get("price_to"), s.get("price_to")) if isinstance(x, (int,float))]
            if caps:
                old["price_to"] = max(caps)
    searches = list(merged.values())

    # Prioritize broad feeds and personal filters first.
    def rank(s):
        q = norm(s.get("query",""))
        broad = q in {"ps5","nintendo switch","3ds","ds lite","ps vita","psp","xbox series s","xbox one x"}
        personal = bool(s.get("_priorite_personnelle"))
        return (0 if personal else 1, 0 if broad else 1, len(q))
    searches.sort(key=rank)
    return searches


def merge_filtres(cfg):
    data = load_json(FILTRES_PATH, {})
    extras = []
    if isinstance(data, dict):
        for key in ("searches", "filtres", "filters"):
            if isinstance(data.get(key), list):
                extras.extend(x for x in data[key] if isinstance(x, dict))
    elif isinstance(data, list):
        extras = [x for x in data if isinstance(x, dict)]
    if extras:
        cfg.setdefault("searches", []).extend(extras)
    return cfg


async def import_user_examples(page, base):
    v8 = base["v8"]
    known_pos = {x.get("url") for x in v8.get("positive_examples", [])}
    known_neg = {x.get("url") for x in v8.get("negative_examples", [])}

    async def process(path, target, known, negative=False, limit=2):
        if not path.exists():
            return 0
        urls = [
            l.strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip().startswith("http") and "/items/" in l
        ]
        count = 0
        for url in urls:
            if url in known or count >= limit:
                continue
            detail = await verify_listing(page, url, "")
            if not detail.get("ok"):
                continue
            ih = download_hash(detail.get("image_url","")) if detail.get("image_url") else ""
            entry = {
                "url": url,
                "title": detail.get("title",""),
                "description": detail.get("text","")[:1800],
                "price": detail.get("price"),
                "image_url": detail.get("image_url",""),
                "image_hash": ih,
                "negative": negative,
                "learned_at": datetime.now().isoformat(timespec="seconds"),
            }
            target.append(entry)
            known.add(url)
            count += 1
            print(f"  + EXEMPLE {'NEGATIF' if negative else 'POSITIF'} V8 | {entry['title'][:60]}")
        return count

    n1 = await process(EXEMPLES_PATH, v8["positive_examples"], known_pos, False, 2)
    n2 = await process(REJETS_PATH, v8["negative_examples"], known_neg, True, 2)
    if n1 or n2:
        save_base(base)


async def recheck_tracked(page, base):
    v8 = base["v8"]
    tracked = v8.get("tracked", {})
    now = datetime.now()
    candidates = []

    for iid, item in tracked.items():
        if item.get("status") != "live":
            continue
        try:
            first = datetime.fromisoformat(item["first_seen"])
            age = (now - first).total_seconds() / 60.0
        except Exception:
            continue
        if 4 <= age <= 35:
            candidates.append((age, iid, item))

    candidates.sort(key=lambda x: x[0], reverse=True)
    changed = False

    for age, iid, item in candidates[:MAX_FAST_RECHECK]:
        detail = await verify_listing(page, item.get("url",""), item.get("title",""))
        if detail.get("ok"):
            item["last_checked"] = datetime.now().isoformat(timespec="seconds")
            favs = detail.get("favourites")
            if favs is not None:
                prev = item.get("favourites")
                item["previous_favourites"] = prev
                item["favourites"] = favs

            if detail.get("sold"):
                mins = round(age, 1)
                item["status"] = "sold_fast" if mins <= FOLLOW_WINDOW_MINUTES else "sold"
                item["sold_minutes"] = mins
                item["description"] = detail.get("text","")[:1800]
                item["image_url"] = detail.get("image_url","")
                if item["status"] == "sold_fast":
                    if not item.get("image_hash") and item.get("image_url"):
                        item["image_hash"] = download_hash(item["image_url"])
                    v8["fast_sales"].append(dict(item))
                    v8["fast_sales"] = v8["fast_sales"][-220:]
                    print(f"  ✅ VENTE RAPIDE APPRISE | {item.get('title','')[:58]} | ~{mins} min")
                changed = True
            elif detail.get("reserved"):
                item["status"] = "reserved"
                item["reserved_minutes"] = round(age, 1)
                changed = True
        else:
            # Disappearance is useful but not called "sold".
            if age <= FOLLOW_WINDOW_MINUTES:
                item["status"] = "disappeared_fast"
                item["disappeared_minutes"] = round(age, 1)
                v8["fast_disappearances"].append(dict(item))
                v8["fast_disappearances"] = v8["fast_disappearances"][-220:]
                print(f"  ? DISPARU RAPIDEMENT | {item.get('title','')[:58]} | ~{age:.1f} min")
                changed = True

    if changed:
        v8["tracked"] = tracked
        save_base(base)


def track_candidate(base, card, search, result, detail=None):
    v8 = base["v8"]
    tracked = v8["tracked"]
    iid = str(card["item_id"])
    now = datetime.now().isoformat(timespec="seconds")
    old = tracked.get(iid, {})
    entry = {
        "item_id": iid,
        "url": card.get("url",""),
        "title": (detail or {}).get("title") or card.get("title",""),
        "category": search.get("category",""),
        "search": search.get("name",""),
        "price": result.get("price"),
        "score_initial": result.get("score"),
        "first_seen": old.get("first_seen", now),
        "last_seen": now,
        "status": old.get("status","live"),
        "favourites": (detail or {}).get("favourites", result.get("favourites")),
        "age_minutes_read": (detail or {}).get("age_minutes", result.get("age_minutes")),
        "description": (detail or {}).get("text","")[:1800],
        "image_url": (detail or {}).get("image_url",""),
        "image_hash": old.get("image_hash",""),
    }
    tracked[iid] = entry
    if len(tracked) > MAX_TRACKED:
        ordered = sorted(tracked.items(), key=lambda kv: kv[1].get("first_seen",""), reverse=True)[:MAX_TRACKED]
        v8["tracked"] = dict(ordered)
    save_base(base)


async def scan_once(page, cfg, blacklist, seen, base):
    searches = compact_searches(cfg)
    print(f"[INFO] V8: {len(searches)} flux compactés, règles précises = bonus seulement.")
    print(f"[INFO] V8: suivi ventes rapides {FOLLOW_WINDOW_MINUTES} min.")

    run_alerts = 0
    dedupe_run = set()
    details_opened = 0
    max_searches = int(cfg.get("v8_max_searches_per_run", 24))
    per_search = int(cfg.get("v8_cards_per_search", 12))
    page_wait_ms = int(cfg.get("page_wait_ms", 900))
    start = time.monotonic()

    previous_tracked = base.get("v8", {}).get("tracked", {})

    for idx, search in enumerate(searches[:max_searches], 1):
        query = search.get("query","")
        cat = search.get("category","")
        cap = search.get("price_to")
        if cap is None:
            cap = CATEGORY_DEFAULT_CAP.get(cat)

        url = f"https://www.vinted.be/catalog?search_text={urllib.parse.quote_plus(query)}&order=newest_first"
        if cap:
            url += f"&price_to={float(cap):g}"

        print(f"\n[SCAN {idx}/{min(len(searches),max_searches)}] {cat} | {query}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=12000)
            await page.wait_for_timeout(page_wait_ms)
            await page.locator('a[href*="/items/"]').first.wait_for(timeout=3000)
        except PlaywrightTimeoutError:
            print("  ! aucun résultat visible")
            continue

        cards = await extract_cards(page)
        fresh = 0
        evaluated = 0
        for card in cards[:per_search]:
            iid = str(card["item_id"])
            if iid in dedupe_run:
                continue
            dedupe_run.add(iid)

            # Newness is based on persisted flux memory, not exact publish time.
            if iid in seen:
                # Update favourite observation if the card still appears.
                old = previous_tracked.get(iid)
                if old:
                    fav = extract_favourites(card.get("text",""))
                    if fav is not None:
                        old["previous_favourites"] = old.get("favourites")
                        old["favourites"] = fav
                        old["last_seen"] = datetime.now().isoformat(timespec="seconds")
                continue

            fresh += 1
            result = score_card(card, search, blacklist, base, previous_tracked.get(iid))
            if not result:
                continue

            if result.get("score",0) <= 0:
                print(f"  X {result.get('reject','rejet')} | {card.get('title','')[:62]}")
                continue

            evaluated += 1
            min_score = CATEGORY_MIN_SCORE.get(cat, 70)
            if result["score"] < min_score:
                continue

            detail = None
            # Notification first. Deep detail is deliberately delayed.
            sent = ntfy_send(card, search, result, None)
            if sent:
                run_alerts += 1
                print(f"  ⚡ ALERTE V8 {result['score']}/100 | {card.get('title','')[:58]} | {result['price']:.2f} EUR")
                print(f"    {card.get('url','')}")

                # Only the best few get deep detail during this fast pass.
                if details_opened < DETAIL_CANDIDATE_LIMIT and result["score"] >= 76:
                    details_opened += 1
                    detail = await verify_listing(page, card["url"], card.get("title",""))
                    if detail.get("ok"):
                        # Re-run hard rejection after full detail.
                        rej, why = hard_reject(
                            detail.get("title",card.get("title","")),
                            detail.get("text",""),
                            cat,
                            blacklist,
                        )
                        if rej:
                            print(f"    ! DETAIL: risque découvert après alerte: {why}")

                        if detail.get("image_url"):
                            ih = download_hash(detail["image_url"])
                            bonus, reason = learned_bonus(
                                detail.get("title",card.get("title","")),
                                result["price"], cat, ih, base
                            )
                            if bonus:
                                print(f"    + apprentissage visuel/titre: {bonus:+d} ({reason})")

                track_candidate(base, card, search, result, detail)

                row = {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "category": cat,
                    "search": f"{search.get('name','')} / V8 FLASH",
                    "brand": "",
                    "model": result.get("matched_label") or "V8 FLASH",
                    "size": "?",
                    "opportunity_score": max(1, min(10, round(result["score"]/10))),
                    "title": (detail or {}).get("title") or card.get("title",""),
                    "listing_price": result["price"],
                    "total_buy_est": result["price"],
                    "resale_low": 0,
                    "resale_high": 0,
                    "margin_low": 0,
                    "margin_high": 0,
                    "roi_low": 0,
                    "reason": result.get("reason",""),
                    "url": card.get("url",""),
                    "item_id": iid,
                }
                append_alert(row)
                append_history({
                    "event": "alert_v8",
                    "item_id": iid,
                    "title": row["title"],
                    "price": result["price"],
                    "score": result["score"],
                    "category": cat,
                    "url": card.get("url",""),
                })

            # Mark every inspected fresh card as seen after the decision.
            seen.add(iid)

        print(f"  FLASH: {fresh} nouvelle(s), {evaluated} évaluée(s), {len(cards[:per_search])} cartes")

        if time.monotonic() - start > 430:
            print("[INFO] Budget V8 atteint, arrêt du scan rapide.")
            break

    save_seen(seen)
    save_base(base)
    return run_alerts


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    print("Vinted V8.0 — FLASH FIRST CLEAN + apprentissage ventes rapides")

    cfg = load_json(CONFIG_PATH, {})
    if not isinstance(cfg, dict) or not cfg:
        print("config.json introuvable ou invalide.")
        sys.exit(1)
    cfg = merge_filtres(cfg)

    blacklist = load_json(BLACKLIST_PATH, {})
    if not isinstance(blacklist, dict):
        blacklist = {}

    seen = load_seen()
    base = load_base()

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / ".pw-profile"),
            headless=True if args.headless or os.getenv("HEADLESS","") else False,
            viewport={"width": 1280, "height": 900},
            locale="fr-BE",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # 1. Learn what disappeared/sold from previous promising alerts.
        await recheck_tracked(page, base)

        # 2. Slowly import at most two positive and two negative manual examples.
        await import_user_examples(page, base)

        # 3. Fast scan. No exact-product rule is required.
        alerts = await scan_once(page, cfg, blacklist, seen, base)

        await context.close()

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] V8 terminé — {alerts} notification(s) article.")


if __name__ == "__main__":
    asyncio.run(main())

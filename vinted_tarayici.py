#!/usr/bin/env python3
# VERSION : VINTED_TARAYICI_V6_7_FINAL
import asyncio
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, unquote

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
ANCIEN_CONFIG_PATH = ROOT / "configuration.json"
BLACKLIST_PATH = ROOT / "blacklist.json"
ANCIEN_BLACKLIST_PATH = ROOT / "liste_noire.json"
SEEN_PATH = ROOT / "annonces_vues.json"
ANCIEN_SEEN_PATH = ROOT / "seen.json"
ALERTS_CSV = ROOT / "alertes.csv"
FILTRES_PATH = ROOT / "filtres.json"
PROFILE_DIR = ROOT / ".profil_vinted"

PRICE_RE = re.compile(
    r"(?:(\d{1,4}(?:[.,]\d{1,2})?)\s*€|€\s*(\d{1,4}(?:[.,]\d{1,2})?))"
)
ITEM_ID_RE = re.compile(r"/items/(\d+)")
SLUG_RE = re.compile(r"/items/\d+-([^/?#]+)")


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def charger_json_avec_ancien_nom(nouveau_fichier, ancien_fichier, valeur_defaut):
    """Charge d'abord le nom français, puis l'ancien nom pour faciliter la migration."""
    if nouveau_fichier.exists():
        return load_json(nouveau_fichier, valeur_defaut)
    if ancien_fichier and ancien_fichier.exists():
        return load_json(ancien_fichier, valeur_defaut)
    return valeur_defaut


def convertir_filtre_personnel(entree):
    """Transforme un filtre simple en une ou plusieurs recherches internes."""
    if not isinstance(entree, dict) or not entree.get("actif", True):
        return []

    nom = str(entree.get("nom", "")).strip()
    categorie = str(entree.get("categorie", "")).strip()

    recherches = entree.get("recherches_vinted", [])
    if isinstance(recherches, str):
        recherches = [recherches]
    recherches = [str(x).strip() for x in recherches if str(x).strip()]

    recherche_unique = str(entree.get("recherche_vinted", "")).strip()
    if recherche_unique:
        recherches.insert(0, recherche_unique)

    recherches = list(dict.fromkeys(recherches))

    if not nom or not recherches or not categorie:
        return []

    revente_basse = entree.get("revente_prudente")
    if revente_basse is None:
        return []

    revente_haute = entree.get("revente_haute", revente_basse)

    regle = {
        "label": nom,
        "brand": str(entree.get("marque", "")).strip(),
        "model": str(entree.get("modele", nom)).strip(),
        "must_contain": list(entree.get("mots_obligatoires", [])),
        "any_contain": list(entree.get("un_des_mots", [])),
        "platform_any": list(entree.get("mots_plateforme", [])),
        "hardware_any": list(entree.get("indices_materiel", [])),
        "exclude": list(entree.get("mots_exclus", [])),
        "exact_title_any": list(entree.get("titres_exacts", [])),
        "resale_low": float(revente_basse),
        "resale_high": float(revente_haute),
        "max_buy_ratio": float(entree.get("ratio_achat_max", 0.40)),
        "min_margin": float(entree.get("marge_minimum", 10)),
        "min_roi_pct": float(entree.get("roi_minimum", 30)),
        "demand_score": int(entree.get("score_demande", 5)),
        "tolerer_fautes": bool(entree.get("tolerer_fautes", True)),
    }

    if entree.get("materiel_dans_titre"):
        regle["hardware_in_title"] = True

    sorties = []
    for recherche in recherches:
        sorties.append({
            "name": f"FILTRE - {nom} / {recherche}",
            "category": categorie,
            "query": recherche,
            "price_to": float(entree.get("prix_recherche_max", revente_basse * 0.50)),
            "max_items": int(entree.get("nombre_annonces_a_lire", 35)),
            "rules": [regle],
            "_priorite_personnelle": bool(entree.get("prioritaire", True)),
        })

    return sorties

def appliquer_filtres_personnels(cfg, blacklist):
    """
    Ajoute les filtres faciles à modifier dans filtres.json.
    Ce fichier permet d'affiner le bot sans toucher au gros configuration.json.
    """
    donnees = load_json(FILTRES_PATH, {})
    if not isinstance(donnees, dict):
        return

    for mot in donnees.get("mots_a_exclure", []):
        if mot and mot not in blacklist.setdefault("accessory_blacklist", []):
            blacklist["accessory_blacklist"].append(mot)

    for mot in donnees.get("mots_a_exclure_du_titre", []):
        if mot and mot not in blacklist.setdefault("title_accessory_blacklist", []):
            blacklist["title_accessory_blacklist"].append(mot)

    recherches_perso = []
    for entree in donnees.get("articles_a_surveille", []):
        recherches = convertir_filtre_personnel(entree)
        if recherches:
            recherches_perso.extend(recherches)

    # Les filtres personnels passent avant les recherches générales.
    cfg["searches"] = recherches_perso + list(cfg.get("searches", []))


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


def parse_price(text):
    vals = []
    for m in PRICE_RE.finditer(text or ""):
        raw = m.group(1) or m.group(2)
        try:
            vals.append(float(raw.replace(",", ".")))
        except ValueError:
            pass
    return min(vals) if vals else None


def fee_estimate(price, cfg):
    bp = cfg.get("buyer_protection_estimate", {})
    return (
        float(bp.get("fixed", 0.70))
        + float(bp.get("pct", 0.05)) * price
        + float(cfg.get("shipping_estimate", 4.50))
    )


def term_present(text, term):
    """Recherche un mot entier pour éviter les faux positifs dans d’autres mots."""
    t = norm(text)
    nt = norm(term)
    if not nt:
        return False
    pattern = rf"(?<!\w){re.escape(nt)}(?!\w)"
    return re.search(pattern, t) is not None


def _mots_similaires(a, b):
    """Tolérance légère aux fautes : 1 lettre, ou 2 sur un mot long."""
    a = norm(a)
    b = norm(b)

    if a == b:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    if any(ch.isdigit() for ch in a + b):
        return False

    limite = 2 if max(len(a), len(b)) >= 8 else 1
    if abs(len(a) - len(b)) > limite:
        return False

    precedent = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        courant = [i]
        minimum_ligne = i
        for j, cb in enumerate(b, 1):
            cout = 0 if ca == cb else 1
            valeur = min(
                courant[j - 1] + 1,
                precedent[j] + 1,
                precedent[j - 1] + cout,
            )
            courant.append(valeur)
            minimum_ligne = min(minimum_ligne, valeur)
        if minimum_ligne > limite:
            return False
        precedent = courant

    return precedent[-1] <= limite


def term_present_souple(text, term):
    """
    Recherche tolérante utilisée seulement pour reconnaître un produit recherché.
    Exemples : nintedo~nintendo, parti~party, swich~switch.
    """
    if term_present(text, term):
        return True

    texte = re.findall(r"[a-z0-9]+", norm(text))
    cible = re.findall(r"[a-z0-9]+", norm(term))

    if not texte or not cible or len(cible) > len(texte):
        return False

    taille = len(cible)
    for i in range(len(texte) - taille + 1):
        bloc = texte[i:i + taille]
        if all(_mots_similaires(x, y) for x, y in zip(bloc, cible)):
            return True

    return False


def keyword_hits(text, words):
    return [w for w in words if term_present(text, w)]


def title_keyword_hits(text, words):
    """Cherche les mots d’accessoires dans le titre sans correspondance partielle."""
    return keyword_hits(text, words)


def clean_title(value):
    value = re.sub(
        r"^(image|photo)\s+(de|of)\s+",
        "",
        (value or "").strip(),
        flags=re.I,
    )
    value = re.sub(r"\s+", " ", value)
    if not 3 <= len(value) <= 220 or "€" in value:
        return ""
    return value


def title_from_card(card):
    for key in ("img_alt", "aria_label", "anchor_title", "anchor_text"):
        value = clean_title(card.get(key, ""))
        if value:
            return value
    m = SLUG_RE.search(card.get("href", ""))
    return (
        unquote(m.group(1)).replace("-", " ")
        if m
        else "Annonce Vinted"
    )


def blacklist_check(title, text, blacklist):
    # V5: reject obvious accessories directly from the listing title.
    # Boundary matching avoids cases such as "case" matching "showcase".
    title_hits = title_keyword_hits(
        title,
        blacklist.get("title_accessory_blacklist", []),
    )

    if title_hits:
        return (
            True,
            "title_accessory_blacklist",
            title_hits[:3],
            [],
        )

    combined = f"{title} {text}"

    for group in (
        "hard_blacklist",
        "fake_blacklist",
        "accessory_blacklist",
    ):
        hits = keyword_hits(
            combined,
            blacklist.get(group, []),
        )
        if hits:
            return True, group, hits[:3], []

    risks = keyword_hits(
        combined,
        blacklist.get("suspicious_words", []),
    )

    return False, "", [], risks[:3]



def low_value_game_check(title, text, blacklist):
    combined = norm(f"{title} {text}")

    low_games = [
        norm(x)
        for x in blacklist.get(
            "low_value_games",
            [],
        )
    ]

    collector_words = [
        norm(x)
        for x in blacklist.get(
            "collector_exception_words",
            [],
        )
    ]

    low_hits = [
        x
        for x in low_games
        if x and term_present(combined, x)
    ]

    if not low_hits:
        return False, []

    has_collector_exception = any(
        x
        for x in collector_words
        if x and term_present(combined, x)
    )

    if has_collector_exception:
        return False, []

    return True, low_hits[:3]


GAME_MERCH_WORDS = [
    "steelbook", "steel book", "pin", "pins", "badge", "coin",
    "pièce de collection", "piece de collection", "médaille", "medaille",
    "keychain", "porte-clés", "porte cles", "porte clé", "porte cle",
    "figurine", "amiibo", "poster", "affiche", "artbook", "art book",
    "soundtrack", "ost", "guide", "manuel seul", "manual only",
    "case only", "game case only", "boite vide", "boîte vide",
    "empty box", "box only", "boitier vide", "boîtier vide",
    "pochette", "housse", "coque", "cover only", "skin", "sticker",
    "carte pokemon", "pokemon card", "trading card"
]

CONSOLE_GAME_WORDS = [
    "jeu", "game", "gioco", "juego", "spiel", "cartouche",
    "cartridge", "videogame", "video game"
]

CONSOLE_ACCESSORY_WORDS = [
    "caricatore", "caricabatterie", "cargador", "chargeur", "charger",
    "batteria", "bateria", "batterie", "battery", "custodia", "funda",
    "housse", "pochette", "coque", "carcasa", "scocca", "shell",
    "stylus", "stylet", "dock", "support", "screen", "écran", "ecran",
    "hub", "hub usb", "hub usb-c", "usb hub", "usb-c hub",
    "joy con", "joy-con", "joycon", "joy cons", "joy-cons", "joycons",
    "manette", "controller", "gamepad", "controle joy-con",
    "contrôle joy-con", "controles joy-con", "contrôles joy-con",
    "cristalbox", "crystalbox", "display case", "boite de protection",
    "boîte de protection", "vitrine de protection"
]


EMPTY_PACKAGING_PHRASES = [
    "boite vide", "boîte vide", "boite seule", "boîte seule",
    "boitier vide", "boîtier vide", "boitier seul", "boîtier seul",
    "emballage vide", "emballage seul", "coffret vide", "coffret seul",
    "sans jeu", "sans cartouche", "sans disque", "sans console",
    "empty box", "box only", "only box", "case only", "only case",
    "empty case", "packaging only", "empty packaging",
    "no game", "no cartridge", "no disc", "no console",
    "leere verpackung", "verpackung leer", "nur verpackung",
    "leere ovp", "ovp leer", "nur ovp", "originalverpackung leer",
    "ohne spiel", "ohne konsole", "ohne gerat", "ohne gerät",
    "scatola vuota", "solo scatola", "custodia vuota", "solo custodia",
    "confezione vuota", "solo confezione", "senza gioco", "senza console",
    "caja vacia", "caja vacía", "solo caja", "caja solamente",
    "estuche vacio", "estuche vacío", "solo estuche",
    "sin juego", "sin consola",
    "lege doos", "doos alleen", "alleen doos", "lege verpakking",
    "alleen verpakking", "zonder spel", "zonder console",
    "caixa vazia", "so caixa", "só caixa", "somente caixa",
    "embalagem vazia", "sem jogo", "sem console",
    "puste pudelko", "puste pudełko", "samo pudelko", "samo pudełko",
    "puste opakowanie", "bez gry", "bez konsoli",
]

PACKAGING_START_WORDS = [
    "boite", "boîte", "boitier", "boîtier", "emballage", "coffret",
    "empty box", "box", "case", "packaging",
    "verpackung", "originalverpackung",
    "scatola", "custodia", "confezione",
    "caja", "estuche", "embalaje",
    "doos", "verpakking",
    "caixa", "embalagem",
    "pudelko", "pudełko", "opakowanie",
]

PACKAGING_SAFE_PHRASES = [
    "avec boite", "avec boîte", "avec boitier", "avec boîtier",
    "with box", "with case", "with packaging",
    "mit ovp", "mit verpackung", "mit originalverpackung",
    "con caja", "con estuche", "con embalaje",
    "con scatola", "con custodia", "con confezione",
    "met doos", "met verpakking",
    "com caixa", "com embalagem",
    "z pudelkiem", "z pudełkiem", "z opakowaniem",
]


def empty_packaging_check(category, title, text=""):
    if not (category.startswith("JEU_") or category == "CONSOLE"):
        return False, []

    title_n = norm(title)
    full_n = norm(f"{title} {text}")

    strong = [x for x in EMPTY_PACKAGING_PHRASES if term_present(full_n, x)]
    if strong:
        return True, strong[:3]

    if any(term_present(title_n, x) for x in PACKAGING_SAFE_PHRASES):
        return False, []

    stripped = title_n.lstrip(" -|:/[]()")
    for word in PACKAGING_START_WORDS:
        w = norm(word)
        if (
            stripped == w
            or stripped.startswith(w + " ")
            or stripped.startswith(w + ":")
            or stripped.startswith(w + "-")
            or stripped.startswith(w + "|")
            or stripped.startswith(w + "/")
        ):
            return True, [word]

    if term_present(title_n, "verpackung") or term_present(title_n, "originalverpackung"):
        return True, ["verpackung"]

    return False, []


CONSOLE_BUNDLE_CUES = [
    "console", "avec", "with", "inclut", "includes", "including",
    "bundle", "pack complet", "complete set", "set complet",
    "fonctionne", "fonctionnel", "working", "tested", "testé", "teste",
]

def _title_starts_with_any(title, terms):
    t = norm(title).lstrip(" -|:/[]()")
    for term in terms:
        nt = norm(term)
        if not nt:
            continue
        if (
            t == nt
            or t.startswith(nt + " ")
            or t.startswith(nt + ":")
            or t.startswith(nt + "-")
            or t.startswith(nt + "|")
            or t.startswith(nt + "/")
        ):
            return True
    return False


def _console_bundle_evidence(title):
    t = norm(title)
    if keyword_hits(t, CONSOLE_BUNDLE_CUES):
        return True
    if "+" in title:
        return True
    if re.search(r"\b(?:128|256|500|512|825|1000|2000)\s*(?:gb|go)\b", t):
        return True
    if re.search(r"\b(?:1|2)\s*tb\b", t):
        return True
    return False


def category_sanity_check(category, title):
    if category.startswith("JEU_"):
        hits = keyword_hits(title, GAME_MERCH_WORDS)
        if hits:
            # Block merchandise/case-only listings. A normal game title
            # containing one of these words is intentionally treated strictly.
            return False, "objet derive/accessoire: " + ", ".join(hits[:3])

    elif category == "CONSOLE":
        accessory_hits = keyword_hits(title, CONSOLE_ACCESSORY_WORDS)
        bundle_evidence = _console_bundle_evidence(title)

        if accessory_hits:
            # Accessory-led titles are always rejected (Hub..., Manette..., Dock...).
            # If the accessory word comes later, allow only when the title
            # clearly describes a console bundle, e.g. "Switch OLED avec chargeur".
            if _title_starts_with_any(title, accessory_hits) or not bundle_evidence:
                return False, "accessoire console: " + ", ".join(accessory_hits[:3])

        game_hits = keyword_hits(title, CONSOLE_GAME_WORDS)
        if game_hits and not bundle_evidence:
            return False, "annonce de jeu, pas console: " + ", ".join(game_hits[:3])

    return True, ""


def exact_title_matches(title, candidates, tolerant=False):
    t = norm(title).strip(" -|:/")
    for candidat in candidates:
        c = norm(candidat).strip(" -|:/")
        if not c:
            continue
        if t == c:
            return True
        if tolerant and term_present_souple(t, c):
            if abs(len(t.split()) - len(c.split())) <= 1:
                return True
    return False


def rule_match(rule, title, text, deep=False):
    title_n = norm(title)
    full_n = norm(f"{title} {text}")

    must = rule.get("must_contain", [])
    any_kw = rule.get("any_contain", [])
    hardware = rule.get("hardware_any", [])
    platform = rule.get("platform_any", [])
    exact_titles = rule.get("exact_title_any", [])
    excludes = rule.get("exclude", [])
    tolerant = bool(rule.get("tolerer_fautes", False))

    positif = term_present_souple if tolerant else term_present

    if must and not all(positif(title_n, x) for x in must):
        return False

    if any_kw and not any(positif(title_n, x) for x in any_kw):
        return False

    # Exclusions et blacklist restent strictes pour éviter les faux positifs.
    if excludes and any(term_present(full_n, x) for x in excludes):
        return False

    if not deep:
        return True

    if platform and not any(positif(full_n, x) for x in platform):
        return False

    if hardware:
        hardware_text = title_n if rule.get("hardware_in_title") else full_n
        has_hardware = any(positif(hardware_text, x) for x in hardware)
        has_exact = (
            exact_title_matches(title, exact_titles, tolerant=tolerant)
            if exact_titles else False
        )
        if not has_hardware and not has_exact:
            return False

    return True

def score_candidate(rule, price, cfg):
    total = price + fee_estimate(price, cfg)

    low = rule.get("resale_low")
    high = rule.get("resale_high")

    if low is None:
        return (
            round(total, 2),
            None,
            None,
            None,
            None,
            None,
        )

    low = float(low)
    high = float(high or low)

    margin_low = low - total
    margin_high = high - total
    roi = (
        margin_low / total * 100
        if total > 0
        else 0
    )

    return (
        round(total, 2),
        low,
        high,
        round(margin_low, 2),
        round(margin_high, 2),
        round(roi, 1),
    )


def extract_size(text):
    raw = text or ""

    patterns = [
        r"\bW\s?(\d{2})\s*[xX/ -]?\s*L\s?(\d{2})\b",
        r"\b(?:EU|EUR|taille|size)\s*[:\-]?\s*(3[4-9]|4[0-9]|5[0-2])\b",
        r"\b(3[4-9]|4[0-9]|5[0-2])\s*(?:EU|EUR)\b",
        r"\b(XXS|XS|S|M|L|XL|XXL|XXXL)\b",
    ]

    m = re.search(patterns[0], raw, flags=re.I)
    if m:
        return f"W{m.group(1)} L{m.group(2)}"

    for p in patterns[1:]:
        m = re.search(p, raw, flags=re.I)
        if m:
            return m.group(1).upper()

    return "?"


def condition_check(text, cfg, rule):
    bad_hits = keyword_hits(
        text,
        cfg.get("fatal_condition_words", []),
    )

    if not bad_hits:
        return True, [], []

    if rule.get("rare_collectible"):
        rare_hits = keyword_hits(
            text,
            cfg.get("rare_exception_words", []),
        )
        min_hits = int(
            cfg.get("rare_exception_min_hits", 2)
        )

        if len(rare_hits) >= min_hits:
            return True, bad_hits[:3], rare_hits[:3]

    return False, bad_hits[:3], []


def ignored_brand_check(text, cfg):
    hits = keyword_hits(
        text,
        cfg.get("ignored_brands", []),
    )
    return hits[:3]



def electronics_condition_check(title, text, cfg, category):
    if category not in {"CONSOLE", "ELECTRONIQUE"}:
        return True, ""

    combined = f"{title} {text}"

    fatal_hits = keyword_hits(
        combined,
        cfg.get(
            "fatal_electronics_condition_words",
            [],
        ),
    )

    if fatal_hits:
        return False, "etat: " + ", ".join(fatal_hits[:3])

    missing_power = keyword_hits(
        combined,
        cfg.get(
            "power_missing_words",
            [],
        ),
    )

    if missing_power:
        return False, "alimentation manquante: " + ", ".join(missing_power[:2])

    return True, ""


def opportunity_score(
    price,
    reference_price,
    margin_low,
    motivation_hits,
    authenticity_risk=False,
    rare_condition=False,
):
    if not reference_price or reference_price <= 0:
        return 1

    ratio = price / reference_price

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

    if authenticity_risk:
        score -= 1

    if rare_condition:
        score -= 1

    score = int(round(score))
    return max(1, min(10, score))


def reason_text(
    price,
    reference_price,
    motivation_hits,
    authenticity_risk=False,
    rare_condition_hits=None,
):
    parts = []

    if reference_price:
        pct = price / reference_price * 100
        parts.append(
            f"prix a environ {pct:.0f}% de la reference prudente"
        )

    if motivation_hits:
        parts.append(
            "vendeur motive: "
            + ", ".join(motivation_hits[:2])
        )

    if authenticity_risk:
        parts.append(
            "authenticite a verifier"
        )

    if rare_condition_hits:
        parts.append(
            "etat atypique tolere seulement car piece rare/vintage"
        )

    if not parts:
        return "rapport achat/revente interessant"

    return "; ".join(parts)


def ntfy_send(row):
    topic = os.getenv("NTFY_TOPIC", "").strip()

    if not topic:
        return False

    server = os.getenv(
        "NTFY_SERVER",
        "https://ntfy.sh",
    ).rstrip("/")

    url = (
        f"{server}/"
        f"{urllib.parse.quote(topic, safe='')}"
    )

    # ASCII uniquement dans les headers HTTP.
    title = (
        f"Vinted Deal {row['opportunity_score']}/10"
    )

    size = row.get("size") or "?"

    condition = (
        "OK"
        if not row.get("risk")
        else "A verifier"
    )

    article_type = row.get(
        "category",
        "ARTICLE",
    )

    body = (
        f"[{row['opportunity_score']}/10] | "
        f"[{article_type}] "
        f"{row.get('brand','?')} "
        f"{row.get('model','?')} "
        f"[{condition}] | "
        f"Achat {row['listing_price']:.2f} EUR "
        f"→ Revente {row['resale_low']:.0f}-"
        f"{row['resale_high']:.0f} EUR | "
        f"Bénéfice net {row['margin_low']:.2f} EUR | "
        f"{row['reason']} | "
        f"{row['url']}"
    )

    headers = {
        "Title": title,
        "Priority": (
            "high"
            if row["opportunity_score"] >= 8
            else "default"
        ),
        "Tags": "moneybag,shopping_cart",
        "Click": row["url"],
        "Actions": (
            f"view, Ouvrir Vinted, {row['url']}"
        ),
    }

    if row.get("image_url"):
        headers["Attach"] = row["image_url"]

    try:
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers=headers,
        )

        with urllib.request.urlopen(
            req,
            timeout=8,
        ) as resp:
            return 200 <= resp.status < 300

    except Exception as e:
        print(f"  ! ntfy: {e}")
        return False


def append_alert(row):
    fields = [
        "timestamp",
        "category",
        "search",
        "brand",
        "model",
        "size",
        "opportunity_score",
        "title",
        "listing_price",
        "total_buy_est",
        "resale_low",
        "resale_high",
        "margin_low",
        "margin_high",
        "roi_low",
        "reason",
        "url",
        "item_id",
    ]

    new = not ALERTS_CSV.exists()

    with ALERTS_CSV.open(
        "a",
        newline="",
        encoding="utf-8-sig",
    ) as f:
        w = csv.DictWriter(
            f,
            fieldnames=fields,
        )

        if new:
            w.writeheader()

        w.writerow(
            {
                k: row.get(k, "")
                for k in fields
            }
        )


async def extract_cards(page):
    data = await page.locator(
        'a[href*="/items/"]'
    ).evaluate_all(
        """
        els => els.map(a => {
          let node = a;
          let text = (a.innerText || '').trim();

          for (
            let i = 0;
            i < 5 && node;
            i++, node = node.parentElement
          ) {
            const t = (node.innerText || '').trim();
            if (
              t.length > text.length
              && t.length < 1200
            ) {
              text = t;
            }
          }

          const img = a.querySelector('img');

          return {
            href: a.href || '',
            text: text,
            anchor_text: (a.innerText || '').trim(),
            aria_label: a.getAttribute('aria-label') || '',
            anchor_title: a.getAttribute('title') || '',
            img_alt: img
              ? (img.getAttribute('alt') || '')
              : ''
          };
        })
        """
    )

    out = []
    already = set()

    for x in data:
        href = x.get("href", "")
        m = ITEM_ID_RE.search(href)

        if not m:
            continue

        item_id = m.group(1)

        if item_id in already:
            continue

        already.add(item_id)

        x["item_id"] = item_id
        x["url"] = href.split("?")[0]
        x["title"] = title_from_card(x)

        out.append(x)

    return out


async def verify_listing(
    page,
    url,
    fallback_title="",
):
    detail = None

    try:
        detail = await page.context.new_page()

        await detail.goto(
            url,
            wait_until="domcontentloaded",
            timeout=9000,
        )

        await detail.wait_for_timeout(350)

        title = fallback_title

        try:
            og_title = await detail.locator(
                'meta[property="og:title"]'
            ).get_attribute("content", timeout=1200)

            cleaned = clean_title(
                og_title or ""
            )

            if cleaned:
                title = cleaned

        except Exception:
            pass

        description_parts = []

        for selector in (
            'meta[property="og:description"]',
            'meta[name="description"]',
        ):
            try:
                value = await detail.locator(
                    selector
                ).first.get_attribute("content", timeout=1200)

                if value:
                    description_parts.append(
                        value
                    )
            except Exception:
                pass

        try:
            main_text = await detail.locator(
                "main"
            ).inner_text(timeout=1800)

            if main_text:
                description_parts.append(
                    main_text[:4500]
                )

        except Exception:
            pass

        full_text = "\n".join(
            description_parts
        )[:7000]

        seller = ""

        try:
            seller_links = detail.locator(
                'a[href*="/member/"]'
            )

            if await seller_links.count() > 0:
                seller = (
                    await seller_links.first.inner_text(timeout=1200)
                ).strip()

        except Exception:
            pass

        image_url = ""

        try:
            image_url = (
                await detail.locator(
                    'meta[property="og:image"]'
                ).get_attribute("content", timeout=1200)
                or ""
            )

        except Exception:
            pass

        detail_price = None

        for selector, attr in (
            ('meta[property="product:price:amount"]', "content"),
            ('meta[itemprop="price"]', "content"),
            ('[itemprop="price"]', "content"),
        ):
            try:
                locator = detail.locator(
                    selector
                ).first

                value = await locator.get_attribute(
                    attr,
                    timeout=900,
                )

                if value:
                    value = value.replace(
                        ",",
                        ".",
                    )
                    m = re.search(
                        r"\d+(?:\.\d{1,2})?",
                        value,
                    )

                    if m:
                        detail_price = float(
                            m.group(0)
                        )
                        break

            except Exception:
                pass

        return {
            "ok": True,
            "title": title,
            "text": full_text,
            "seller": seller,
            "image_url": image_url,
            "price": detail_price,
        }

    except PlaywrightTimeoutError:
        return {
            "ok": False,
            "title": fallback_title,
            "text": "",
            "seller": "",
            "image_url": "",
            "price": None,
            "error": "timeout annonce",
        }

    except Exception as e:
        return {
            "ok": False,
            "title": fallback_title,
            "text": "",
            "seller": "",
            "image_url": "",
            "price": None,
            "error": str(e)[:120],
        }

    finally:
        if detail:
            try:
                await detail.close()
            except Exception:
                pass


def suspicious_price(rule, price):
    resale_low = rule.get(
        "resale_low"
    )

    if resale_low is None:
        return ""

    resale_low = float(
        resale_low
    )

    threshold = float(
        rule.get(
            "suspicious_price_ratio",
            0.18,
        )
    )

    if price <= resale_low * threshold:
        return "prix anormalement bas"

    return ""


async def scan_search(
    page,
    search,
    cfg,
    blacklist,
    seen_ids,
):
    base = cfg.get(
        "base_url",
        "https://www.vinted.be",
    ).rstrip("/")

    query = search["query"]
    price_to = search.get(
        "price_to"
    )

    url = (
        f"{base}/catalog?"
        f"search_text={quote_plus(query)}"
        f"&order=newest_first"
    )

    if price_to is not None:
        url += (
            f"&price_to="
            f"{float(price_to):g}"
        )

    print(
        f"\n[SCAN] "
        f"{search['name']} -> {url}"
    )

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=14000,
        )

        await page.wait_for_timeout(
            int(
                cfg.get(
                    "page_wait_ms",
                    1800,
                )
            )
        )

        await page.locator(
            'a[href*="/items/"]'
        ).first.wait_for(
            timeout=4000
        )

    except PlaywrightTimeoutError:
        print(
            "  ! Aucun résultat visible "
            "/ contrôle Vinted possible."
        )
        return []

    cards = await extract_cards(
        page
    )

    new_alerts = []

    max_items = int(
        search.get(
            "max_items",
            cfg.get(
                "max_items_per_search",
                40,
            ),
        )
    )

    global_min_roi = float(
        cfg.get(
            "min_roi_pct",
            20,
        )
    )

    global_min_demand = int(
        cfg.get(
            "min_demand_score",
            0,
        )
    )

    for c in cards[:max_items]:
        if c["item_id"] in seen_ids:
            continue

        title = c["title"]
        text = c.get(
            "text",
            "",
        )

        ignored_hits = ignored_brand_check(
            f"{title} {text}",
            cfg,
        )

        if ignored_hits:
            print(
                f"  X MARQUE IGNORÉE | "
                f"{title[:65]} | "
                f"{ignored_hits}"
            )
            continue

        price = parse_price(
            text
        )

        if price is None:
            continue

        if (
            price_to is not None
            and price
            > float(price_to) * 1.05
        ):
            continue

        blocked, group, hits, risks = (
            blacklist_check(
                title,
                text,
                blacklist,
            )
        )

        if blocked:
            print(
                f"  X BLACKLIST "
                f"[{group}] "
                f"{title[:65]} | "
                f"{hits}"
            )
            continue

        low_value_blocked, low_value_hits = (
            low_value_game_check(
                title,
                text,
                blacklist,
            )
        )

        if low_value_blocked:
            print(
                f"  X JEU FAIBLE VALEUR | "
                f"{title[:65]} | "
                f"{low_value_hits}"
            )
            continue

        category = search.get("category", "")
        sane, sane_reason = category_sanity_check(category, title)
        if not sane:
            print(
                f"  X TYPE PRODUIT | "
                f"{title[:65]} | {sane_reason}"
            )
            continue

        packaging_only, packaging_hits = empty_packaging_check(
            category, title, text
        )
        if packaging_only:
            print(
                f"  X BOITE/EMBALLAGE SEUL | "
                f"{title[:65]} | {packaging_hits}"
            )
            continue

        matched_rule = None

        for rule in search.get(
            "rules",
            [],
        ):
            if rule_match(
                rule,
                title,
                text,
            ):
                matched_rule = rule
                break

        if matched_rule is None:
            continue

        (
            total,
            resale_low,
            resale_high,
            margin_low,
            margin_high,
            roi_low,
        ) = score_candidate(
            matched_rule,
            price,
            cfg,
        )

        if margin_low is None:
            continue

        reference_price = float(
            matched_rule.get(
                "market_avg",
                resale_low,
            )
        )

        max_buy_ratio = (
            matched_rule.get(
                "max_buy_ratio",
                cfg.get(
                    "max_buy_ratio_default",
                    0.40,
                ),
            )
        )

        if (
            max_buy_ratio is not None
            and reference_price > 0
            and price
            > reference_price
            * float(max_buy_ratio)
        ):
            continue

        category_min_margin = (
            cfg.get(
                "category_min_margin",
                {},
            ).get(
                category,
                cfg.get(
                    "min_margin",
                    25,
                ),
            )
        )

        min_margin = float(
            matched_rule.get(
                "min_margin",
                category_min_margin,
            )
        )

        min_roi = float(
            matched_rule.get(
                "min_roi_pct",
                global_min_roi,
            )
        )

        demand_score = int(
            matched_rule.get(
                "demand_score",
                3,
            )
        )

        if (
            demand_score
            < global_min_demand
        ):
            continue

        if margin_low < min_margin:
            continue

        if roi_low < min_roi:
            continue

        detail = await verify_listing(
            page,
            c["url"],
            title,
        )

        if not detail.get("ok"):
            print(
                f"  ? VERIFICATION "
                f"IMPOSSIBLE | "
                f"{title[:60]} | "
                f"{c['url']}"
            )
            continue

        # This listing was successfully inspected in depth.
        # Avoid repeating the same costly verification on every scheduled run.
        seen_ids.add(c["item_id"])

        verified_title = (
            detail.get("title")
            or title
        )

        verified_text = (
            detail.get("text")
            or text
        )

        seller = (
            detail.get(
                "seller",
                "",
            )
            .strip()
        )

        actual_price = detail.get(
            "price"
        )

        if (
            actual_price is not None
            and actual_price > 0
            and (
                price_to is None
                or actual_price
                <= float(price_to) * 1.10
            )
        ):
            price = actual_price

            (
                total,
                resale_low,
                resale_high,
                margin_low,
                margin_high,
                roi_low,
            ) = score_candidate(
                matched_rule,
                price,
                cfg,
            )

            reference_price = float(
                matched_rule.get(
                    "market_avg",
                    resale_low,
                )
            )

        deep_blocked, deep_group, deep_hits, deep_risks = (
            blacklist_check(
                verified_title,
                verified_text,
                blacklist,
            )
        )

        if deep_blocked:
            print(
                f"  X REJET APRES "
                f"VERIFICATION "
                f"[{deep_group}] "
                f"{verified_title[:60]} | "
                f"{deep_hits}"
            )
            continue

        low_value_blocked, low_value_hits = (
            low_value_game_check(
                verified_title,
                verified_text,
                blacklist,
            )
        )

        if low_value_blocked:
            print(
                f"  X JEU FAIBLE VALEUR "
                f"APRES VERIFICATION | "
                f"{low_value_hits}"
            )
            continue

        ignored_hits = ignored_brand_check(
            f"{verified_title} "
            f"{verified_text}",
            cfg,
        )

        if ignored_hits:
            print(
                f"  X MARQUE IGNORÉE "
                f"APRES VERIFICATION | "
                f"{ignored_hits}"
            )
            continue

        condition_ok, bad_condition_hits, rare_hits = (
            condition_check(
                f"{verified_title} "
                f"{verified_text}",
                cfg,
                matched_rule,
            )
        )

        if not condition_ok:
            print(
                f"  X ETAT REDHIBITOIRE | "
                f"{verified_title[:60]} | "
                f"{bad_condition_hits}"
            )
            continue

        seller_blacklist = [
            norm(x)
            for x in blacklist.get(
                "seller_blacklist",
                [],
            )
        ]

        if (
            seller
            and norm(seller)
            in seller_blacklist
        ):
            print(
                f"  X VENDEUR "
                f"BLACKLISTE | "
                f"{seller}"
            )
            continue

        sane, sane_reason = category_sanity_check(category, verified_title)
        if not sane:
            print(
                f"  X TYPE PRODUIT APRES VERIFICATION | "
                f"{verified_title[:65]} | {sane_reason}"
            )
            continue

        packaging_only, packaging_hits = empty_packaging_check(
            category, verified_title, verified_text
        )
        if packaging_only:
            print(
                f"  X BOITE/EMBALLAGE SEUL APRES VERIFICATION | "
                f"{verified_title[:65]} | {packaging_hits}"
            )
            continue

        if not rule_match(
            matched_rule,
            verified_title,
            verified_text,
            deep=True,
        ):
            print(
                f"  X MAUVAIS PRODUIT | "
                f"{verified_title[:65]}"
            )
            continue

        electronics_ok, electronics_reason = (
            electronics_condition_check(
                verified_title,
                verified_text,
                cfg,
                search.get(
                    "category",
                    "",
                ),
            )
        )

        if not electronics_ok:
            print(
                f"  X ETAT/ALIMENTATION | "
                f"{electronics_reason}"
            )
            continue

        if (
            max_buy_ratio is not None
            and reference_price > 0
            and price
            > reference_price
            * float(max_buy_ratio)
        ):
            continue

        if margin_low < min_margin:
            continue

        if roi_low < min_roi:
            continue

        title = verified_title
        text = verified_text

        risks = list(
            dict.fromkeys(
                list(risks)
                + list(deep_risks)
            )
        )

        motivation_hits = keyword_hits(
            f"{title} {text}",
            cfg.get(
                "seller_motivation_words",
                [],
            ),
        )

        rare_condition = bool(
            bad_condition_hits
            and rare_hits
        )

        score = opportunity_score(
            price,
            reference_price,
            margin_low,
            motivation_hits,
            authenticity_risk=bool(
                matched_rule.get(
                    "authenticity_risk"
                )
            ),
            rare_condition=rare_condition,
        )

        reason = reason_text(
            price,
            reference_price,
            motivation_hits,
            authenticity_risk=bool(
                matched_rule.get(
                    "authenticity_risk"
                )
            ),
            rare_condition_hits=(
                bad_condition_hits
                if rare_condition
                else None
            ),
        )

        abnormal = suspicious_price(
            matched_rule,
            price,
        )

        if abnormal:
            reason += (
                "; prix extrêmement bas, "
                "verifier vendeur et authenticite"
            )
            risks = list(dict.fromkeys(risks + ["prix anormalement bas"]))
            score = min(score, 6)

        size = extract_size(
            f"{title} {text}"
        )

        row = {
            "timestamp": datetime.now().isoformat(
                timespec="seconds"
            ),
            "category": search.get(
                "category",
                "",
            ),
            "search": (
                search["name"]
                + " / "
                + matched_rule.get(
                    "label",
                    "",
                )
            ),
            "brand": matched_rule.get(
                "brand",
                search.get(
                    "name",
                    "",
                ),
            ),
            "model": matched_rule.get(
                "model",
                matched_rule.get(
                    "label",
                    "",
                ),
            ),
            "size": size,
            "opportunity_score": score,
            "title": title,
            "image_url": detail.get(
                "image_url",
                "",
            ),
            "listing_price": round(
                price,
                2,
            ),
            "total_buy_est": total,
            "resale_low": resale_low,
            "resale_high": resale_high,
            "margin_low": margin_low,
            "margin_high": margin_high,
            "roi_low": roi_low,
            "demand_score": demand_score,
            "risk": ", ".join(
                dict.fromkeys(risks)
            ),
            "reason": reason,
            "url": c["url"],
            "item_id": c["item_id"],
        }

        append_alert(
            row
        )

        new_alerts.append(
            row
        )

        ntfy_send(
            row
        )

        print(
            f"  ★ SCORE {score}/10 | "
            f"{title[:58]} | "
            f"{price:.2f} EUR | "
            f"marge +{margin_low:.2f} EUR"
        )

        print(
            f"    {c['url']}"
        )

    for row in new_alerts:
        seen_ids.add(
            row["item_id"]
        )

    return new_alerts


async def main():
    cfg = charger_json_avec_ancien_nom(
        CONFIG_PATH,
        ANCIEN_CONFIG_PATH,
        {},
    )

    blacklist = charger_json_avec_ancien_nom(
        BLACKLIST_PATH,
        ANCIEN_BLACKLIST_PATH,
        {},
    )

    appliquer_filtres_personnels(
        cfg,
        blacklist,
    )

    if not cfg:
        print(
            "config.json introuvable."
        )
        sys.exit(1)

    if not blacklist:
        print(
            "ATTENTION: blacklist.json "
            "introuvable ou vide."
        )

    seen_ids = set(
        charger_json_avec_ancien_nom(
            SEEN_PATH,
            ANCIEN_SEEN_PATH,
            [],
        )
    )

    one_shot = (
        "--once"
        in sys.argv
    )

    headless_arg = (
        "--headless"
        in sys.argv
    )

    print(
        "Vinted Tarayici V6.7"
    )

    print(
        "Mode opportunités : "
        "prix/revente + marge + "
        "etat + vendeur motive + "
        "anti faux-positifs."
    )

    async with async_playwright() as p:
        env_headless = (
            os.getenv(
                "HEADLESS",
                "",
            )
            .strip()
            .lower()
            in {
                "1",
                "true",
                "yes",
                "on",
            }
        )

        effective_headless = (
            headless_arg
            or env_headless
        )

        context = (
            await p.chromium.launch_persistent_context(
                user_data_dir=str(
                    PROFILE_DIR
                ),
                headless=effective_headless,
                viewport={
                    "width": 1280,
                    "height": 900,
                },
                locale="fr-BE",
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
        )

        page = (
            context.pages[0]
            if context.pages
            else await context.new_page()
        )

        while True:
            cycle_alerts = 0
            cycle_start = time.monotonic()

            searches = list(
                cfg.get(
                    "searches",
                    [],
                )
            )

            # Les filtres personnels prioritaires sont vérifiés à chaque passage.
            # Les autres recherches tournent pour répartir le temps disponible.
            prioritaires = [
                s for s in searches
                if s.get("_priorite_personnelle")
            ]
            normales = [
                s for s in searches
                if not s.get("_priorite_personnelle")
            ]

            if normales:
                slot = int(time.time() // 300)
                start_index = slot % len(normales)
                normales = (
                    normales[start_index:]
                    + normales[:start_index]
                )

            searches = prioritaires + normales

            # Garde une marge de sécurité sous la limite GitHub Actions de 15 minutes.
            # La durée peut être modifiée dans configuration.json.
            run_budget_seconds = float(
                cfg.get(
                    "run_budget_seconds",
                    480,
                )
            )

            scanned_searches = 0

            for search in searches:
                elapsed = time.monotonic() - cycle_start
                if one_shot and elapsed >= run_budget_seconds:
                    print(
                        f"\n[INFO] Budget du scan atteint "
                        f"({elapsed:.0f}s). "
                        f"Les autres recherches passeront "
                        f"au prochain run."
                    )
                    break

                try:
                    alerts = await scan_search(
                        page,
                        search,
                        cfg,
                        blacklist,
                        seen_ids,
                    )

                    scanned_searches += 1
                    cycle_alerts += len(
                        alerts
                    )

                    save_json(
                        SEEN_PATH,
                        sorted(
                            seen_ids
                        ),
                    )

                    await asyncio.sleep(
                        float(
                            cfg.get(
                                "delay_between_searches",
                                1,
                            )
                        )
                    )

                except Exception as e:
                    print(
                        f"  ! Erreur "
                        f"{search.get('name')}: "
                        f"{e}"
                    )

            print(
                f"\n["
                f"{datetime.now().strftime('%H:%M:%S')}"
                f"] Cycle termine — "
                f"{cycle_alerts} "
                f"nouvelle(s) alerte(s), "
                f"{scanned_searches} recherche(s)."
            )

            save_json(
                SEEN_PATH,
                sorted(
                    seen_ids
                ),
            )

            if one_shot:
                break

            await asyncio.sleep(
                float(
                    cfg.get(
                        "poll_seconds",
                        300,
                    )
                )
            )

        await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(
            main()
        )
    except KeyboardInterrupt:
        print(
            "\nArret demande."
        )

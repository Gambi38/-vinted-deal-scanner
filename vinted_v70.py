#!/usr/bin/env python3
# VERSION : VINTED_V70_MEMOIRE_PERSISTANTE
#
# Surcouche V6.9 :
# - ouvre chaque lien de exemples.txt
# - apprend le vrai titre, le prix et la description
# - déduit le type de produit et la plateforme
# - utilise le prix de l'exemple comme référence de "bon achat"
# - renforce fortement les filtres anti faux-positifs
#
# Ce fichier importe vinted_tarayici.py et améliore son comportement
# sans supprimer config.json, filtres.json ou blacklist.json.

import asyncio
import csv
import hashlib
import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import vinted_tarayici as vt

# ---------------------------------------------------------------------------
# V7.0 - MEMOIRE PERSISTANTE
# ---------------------------------------------------------------------------
BASE_APPRENTISSAGE = vt.ROOT / "base_apprentissage.json"
EXEMPLES_CLASSES = vt.ROOT / "exemples_classes.txt"
HISTORIQUE_ANNONCES = vt.ROOT / "historique_annonces.jsonl"

CACHE_DETAILS = {}
HISTORIQUE_IDS = set()

STOPWORDS_PROFIL = {
    "avec", "pour", "dans", "this", "that", "the", "and", "und", "con",
    "una", "uno", "del", "della", "des", "les", "une", "sur", "vinted",
    "etat", "état", "bon", "bonne", "tres", "très", "comme", "vend",
    "vente", "article", "produit", "neuf", "neuve", "excellent", "condition",
}


def charger_base():
    if not BASE_APPRENTISSAGE.exists():
        return {
            "version": 1,
            "profils": {},
            "liens_classes": [],
            "historique_importe": [],
        }
    try:
        data = json.loads(BASE_APPRENTISSAGE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("base invalide")
    except Exception:
        data = {}

    data.setdefault("version", 1)
    data.setdefault("profils", {})
    data.setdefault("liens_classes", [])
    data.setdefault("historique_importe", [])
    return data


def sauver_base(data):
    tmp = BASE_APPRENTISSAGE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(BASE_APPRENTISSAGE)


def profil_id(type_produit, plateforme, modele, mots):
    brut = "|".join([
        n(type_produit),
        n(plateforme),
        n(modele),
        " ".join(n(x) for x in mots[:5]),
    ])
    return hashlib.sha1(brut.encode("utf-8")).hexdigest()[:14]


def mots_signature(description, titre=""):
    texte = n(f"{titre} {titre} {description}")
    mots = re.findall(r"[a-z0-9]{4,}", texte)
    compte = Counter(
        x for x in mots
        if x not in STOPWORDS_PROFIL
        and x not in vt.MOTS_GENERIQUES_EXEMPLE
    )
    return [mot for mot, _ in compte.most_common(24)]


def classer_lien(url, profil):
    base = charger_base()
    classes = base.setdefault("liens_classes", [])

    if url and url not in classes:
        classes.append(url)
        sauver_base(base)

        ligne = (
            f"{datetime.now().isoformat(timespec='seconds')} | "
            f"{profil.get('profil_id','')} | "
            f"{profil.get('type_produit','')} | "
            f"{profil.get('plateforme','')} | "
            f"{profil.get('prix_exemple','?')} EUR | "
            f"{url}\n"
        )
        with EXEMPLES_CLASSES.open("a", encoding="utf-8") as f:
            f.write(ligne)


def sauvegarder_profil_exemple(search, rule, url, titre, texte, prix):
    base = charger_base()
    profils = base.setdefault("profils", {})

    type_produit = rule.get("_type_produit", "")
    plateforme = rule.get("_plateforme_apprise", "")
    modele = rule.get("model", titre)
    coeur = list(dict.fromkeys(
        list(rule.get("must_contain", []))
        + list(rule.get("any_contain", []))
    ))

    pid = profil_id(type_produit, plateforme, modele, coeur)
    ancien = profils.get(pid, {})

    liens = list(ancien.get("liens_exemples", []))
    if url and url not in liens:
        liens.append(url)

    prix_exemples = [
        float(x) for x in ancien.get("prix_exemples", [])
        if isinstance(x, (int, float)) and x > 0
    ]
    if prix and prix > 0:
        p = round(float(prix), 2)
        if p not in prix_exemples:
            prix_exemples.append(p)

    if prix_exemples:
        mediane = statistics.median(prix_exemples)
        # Les liens donnés par l'utilisateur sont des exemples POSITIFS :
        # plafond proche du meilleur prix observé, avec petite tolérance.
        plafond = min(
            max(prix_exemples) * 1.18,
            mediane * 1.35,
        )
        plafond = max(plafond, mediane + 3.0)
    else:
        mediane = None
        plafond = None

    signatures = list(dict.fromkeys(
        list(ancien.get("mots_description", []))
        + mots_signature(texte, titre)
    ))[:32]

    profil = {
        "profil_id": pid,
        "actif": True,
        "type_produit": type_produit,
        "categorie": search.get("category", ""),
        "plateforme": plateforme,
        "modele": modele,
        "titre_reference": titre,
        "requete": search.get("query", ""),
        "mots_obligatoires": list(rule.get("must_contain", [])),
        "mots_secondaires": list(rule.get("any_contain", [])),
        "mots_description": signatures,
        "mots_exclus": list(rule.get("exclude", [])),
        "aliases_console": list(rule.get("_aliases_console", [])),
        "prix_exemples": sorted(prix_exemples),
        "prix_cible_median": round(mediane, 2) if mediane is not None else None,
        "prix_cible_max": round(plafond, 2) if plafond is not None else None,
        "prix_marche_bas": ancien.get("prix_marche_bas"),
        "prix_marche_haut": ancien.get("prix_marche_haut"),
        "liens_exemples": liens[-20:],
        "nombre_exemples": len(liens),
        "description_reference": (texte or "")[:1800],
        "mis_a_jour": datetime.now().isoformat(timespec="seconds"),
    }

    profils[pid] = profil
    sauver_base(base)

    rule["_profil_id"] = pid
    rule["_mots_description"] = signatures
    rule["_prix_cible_max"] = profil["prix_cible_max"]

    classer_lien(url, profil)
    return profil


def score_ressemblance_profil(profil, titre, texte, prix=None, deep=False):
    titre_n = n(titre)
    full_n = n(f"{titre} {texte}")

    obligatoires = profil.get("mots_obligatoires", [])
    secondaires = profil.get("mots_secondaires", [])
    desc = profil.get("mots_description", [])

    if obligatoires:
        nb = sum(1 for x in obligatoires if vt.term_present_souple(titre_n, x))
        titre_score = nb / max(1, len(obligatoires))
    else:
        titre_score = 0.0

    if secondaires:
        nb2 = sum(1 for x in secondaires if vt.term_present_souple(titre_n, x))
        secondaire_score = nb2 / max(1, len(secondaires))
    else:
        secondaire_score = 1.0

    desc_score = 0.0
    if deep and desc:
        trouve = sum(1 for x in desc[:16] if present(full_n, x))
        desc_score = min(1.0, trouve / 4.0)

    prix_score = 1.0
    plafond = profil.get("prix_cible_max")
    if prix is not None and plafond:
        ratio = float(prix) / max(1.0, float(plafond))
        if ratio <= 1.0:
            prix_score = 1.0
        elif ratio <= 1.20:
            prix_score = 0.5
        else:
            prix_score = 0.0

    score = (
        titre_score * 0.55
        + secondaire_score * 0.15
        + desc_score * 0.15
        + prix_score * 0.15
    )
    return round(score, 3)


def recherche_depuis_profil(profil):
    rule = {
        "label": f"Profil appris : {profil.get('modele','')}",
        "brand": "",
        "model": profil.get("modele", ""),
        "must_contain": list(profil.get("mots_obligatoires", [])),
        "any_contain": list(profil.get("mots_secondaires", [])),
        "exclude": list(profil.get("mots_exclus", [])),
        "platform_any": PLATEFORMES.get(profil.get("plateforme", ""), []),
        "hardware_any": [],
        "resale_low": profil.get("prix_marche_bas"),
        "resale_high": profil.get("prix_marche_haut"),
        "max_buy_ratio": 0.55,
        "min_margin": 7 if profil.get("type_produit") == "jeu" else 12,
        "min_roi_pct": 28 if profil.get("type_produit") == "jeu" else 30,
        "demand_score": min(10, 5 + int(profil.get("nombre_exemples", 1) >= 2)),
        "tolerer_fautes": True,
        "auto_market": True,
        "_appris_detail": True,
        "_profil_db": True,
        "_profil_id": profil.get("profil_id"),
        "_type_produit": profil.get("type_produit", ""),
        "_plateforme_apprise": profil.get("plateforme", ""),
        "_aliases_console": list(profil.get("aliases_console", [])),
        "_mots_description": list(profil.get("mots_description", [])),
        "_prix_cible_max": profil.get("prix_cible_max"),
    }

    return {
        "name": f"BASE - {profil.get('modele','')}",
        "category": profil.get("categorie", "JEU_EXEMPLE"),
        "query": profil.get("requete") or profil.get("modele", ""),
        # Pas de plafond ici : on veut encore voir le marché pour recalibrer.
        # Le plafond "pépite" est appliqué après estimation.
        "price_to": None,
        "max_items": 45,
        "rules": [rule],
        "_exemple_appris": True,
        "_profil_db": True,
    }


def ajouter_profils_db_aux_recherches(cfg):
    base = charger_base()
    ajouts = []

    for profil in base.get("profils", {}).values():
        if not profil.get("actif", True):
            continue
        if not profil.get("requete"):
            continue
        ajouts.append(recherche_depuis_profil(profil))

    if ajouts:
        # Les profils appris ont priorité, mais restent distincts des filtres manuels.
        cfg["searches"] = ajouts + list(cfg.get("searches", []))

    return len(ajouts)


def liens_deja_classes():
    base = charger_base()
    return set(base.get("liens_classes", []))


def appliquer_exemples_v70(cfg):
    # Les liens déjà appris restent dans exemples.txt si l'utilisateur veut,
    # mais ils ne sont PLUS ouverts ni réappris à chaque passage.
    classes = liens_deja_classes()
    recherches = []

    for url in _ancien_lire_exemples():
        if url in classes:
            continue
        recherche = vt.convertir_exemple_en_recherche(url)
        if recherche:
            recherches.append(recherche)

    if recherches:
        cfg["searches"] = recherches + list(cfg.get("searches", []))

    nb_profils = ajouter_profils_db_aux_recherches(cfg)
    if nb_profils:
        print(f"[INFO] {nb_profils} profil(s) permanent(s) chargé(s) depuis la base.")

    return len(recherches)


def historique_charge():
    global HISTORIQUE_IDS
    if HISTORIQUE_IDS:
        return

    if not HISTORIQUE_ANNONCES.exists():
        return

    try:
        for ligne in HISTORIQUE_ANNONCES.read_text(
            encoding="utf-8"
        ).splitlines():
            try:
                obj = json.loads(ligne)
                if obj.get("item_id"):
                    HISTORIQUE_IDS.add(str(obj["item_id"]))
            except Exception:
                pass
    except Exception:
        pass


def ajouter_historique(obj):
    historique_charge()
    item_id = str(obj.get("item_id") or "")
    cle = item_id or hashlib.sha1(
        (str(obj.get("url","")) + str(obj.get("title",""))).encode("utf-8")
    ).hexdigest()[:14]

    if cle in HISTORIQUE_IDS:
        return

    obj["enregistre_le"] = datetime.now().isoformat(timespec="seconds")
    with HISTORIQUE_ANNONCES.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    HISTORIQUE_IDS.add(cle)


def importer_alertes_csv_existantes():
    if not vt.ALERTS_CSV.exists():
        return

    try:
        with vt.ALERTS_CSV.open(
            "r", encoding="utf-8-sig", newline=""
        ) as f:
            for row in csv.DictReader(f):
                ajouter_historique({
                    "origine": "ancienne_alerte_non_validee",
                    "confiance": 0.20,
                    "item_id": row.get("item_id", ""),
                    "url": row.get("url", ""),
                    "title": row.get("title", ""),
                    "price": row.get("listing_price", ""),
                    "category": row.get("category", ""),
                    "model": row.get("model", ""),
                    # IMPORTANT : ancienne alerte != exemple positif.
                    "positif_utilisateur": False,
                })
    except Exception as e:
        print(f"[INFO] Historique CSV non importé: {e}")


async def verify_listing_v70(page, url, fallback_title=""):
    detail = await _ancien_verify_listing(page, url, fallback_title)
    if detail.get("ok"):
        CACHE_DETAILS[url.split("?")[0]] = detail
    return detail


def append_alert_v70(row):
    _ancien_append_alert(row)

    url = str(row.get("url", "")).split("?")[0]
    detail = CACHE_DETAILS.get(url, {})

    ajouter_historique({
        "origine": "alerte_scanner",
        "confiance": 0.35,
        "positif_utilisateur": False,
        "item_id": row.get("item_id", ""),
        "url": url,
        "title": row.get("title", ""),
        "price": row.get("listing_price"),
        "category": row.get("category", ""),
        "model": row.get("model", ""),
        "score": row.get("opportunity_score"),
        "description": (detail.get("text") or "")[:1800],
    })


def n(s):
    return vt.norm(s or "")


def present(text, term):
    t = n(text)
    q = n(term)
    if not q:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(q) + r"(?![a-z0-9])", t) is not None


def hits(text, terms):
    return [x for x in terms if present(text, x)]


# Objets qui utilisent le nom du jeu mais qui ne sont PAS le jeu.
DERIVES_JEU = [
    "medallion", "médaillon", "medaillon",
    "pierre sacrée", "pierre sacree", "sacred stone",
    "pierre zelda", "stone zelda",
    "plv", "présentoir", "presentoir", "display stand", "standee",
    "décoration", "decoration", "déco", "deco",
    "lithographie", "lithograph", "art card", "carte art",
    "porte clé", "porte cle", "porte-clés", "porte cles", "keychain",
    "pin", "pins", "badge", "coin", "pièce de collection", "piece de collection",
    "médaille", "medaille", "figurine", "amiibo", "poster", "affiche",
    "steelbook", "steel book", "artbook", "art book", "soundtrack", "ost",
    "guide", "manuel seul", "manual only",
    "boite vide", "boîte vide", "boite seule", "boîte seule",
    "boitier vide", "boîtier vide", "case only", "box only", "empty box",
    "lot de boites", "lot de boîtes", "lot boites", "lot boîtes",
    "boites nintendo", "boîtes nintendo", "boites switch", "boîtes switch",
    "pochette", "housse", "coque", "skin", "sticker",
]

# Pièces / accessoires qui se font passer pour des consoles.
PIECES_CONSOLE = [
    "ricambi", "ricambio", "pezzi di ricambio",
    "pour pièces", "pour pieces", "pièces détachées", "pieces detachees",
    "spare parts", "for parts", "parts only",
    "carte r4", "r4 card", "flashcard", "flash card",
    "tarjeta", "409 juegos", "208 juegos", "500 juegos", "jeux intégrés",
    "jeux integres", "multicart", "multi cart",
    "motherboard", "carte mère", "carte mere",
    "port ds", "port cartouche", "lecteur cartouche",
    "écran seul", "ecran seul", "screen only",
    "coque seule", "shell only", "chassis seul",
]

# Accessoires acceptables quand ils sont INCLUS avec un vrai produit.
ACCESSOIRES_BUNDLE_OK = [
    "chargeur", "charger", "cargador", "caricatore", "caricabatterie",
    "câble", "cable", "usb c", "usb-c",
    "housse", "pochette", "étui", "etui", "custodia", "funda",
    "batterie", "battery", "bateria", "batteria",
    "dock", "station d'accueil", "station accueil",
    "manette", "controller", "joycon", "joy-con", "joy con",
    "stylet", "stylus",
]

CONSOLES = [
    ("new nintendo 3ds xl", ["new nintendo 3ds xl", "new 3ds xl"]),
    ("new nintendo 3ds", ["new nintendo 3ds", "new 3ds"]),
    ("nintendo 3ds xl", ["nintendo 3ds xl", "3ds xl"]),
    ("nintendo 3ds", ["nintendo 3ds", "3ds"]),
    ("nintendo 2ds xl", ["nintendo 2ds xl", "2ds xl"]),
    ("nintendo 2ds", ["nintendo 2ds", "2ds"]),
    ("nintendo ds lite", ["nintendo ds lite", "ds lite"]),
    ("nintendo dsi xl", ["nintendo dsi xl", "dsi xl"]),
    ("nintendo dsi", ["nintendo dsi", "dsi"]),
    ("nintendo switch oled", ["nintendo switch oled", "switch oled"]),
    ("nintendo switch lite", ["nintendo switch lite", "switch lite"]),
    ("nintendo switch", ["nintendo switch", "switch"]),
    ("playstation 5 slim", ["playstation 5 slim", "ps5 slim"]),
    ("playstation 5", ["playstation 5", "ps5"]),
    ("playstation 4 pro", ["playstation 4 pro", "ps4 pro"]),
    ("playstation 4", ["playstation 4", "ps4"]),
    ("ps vita", ["ps vita", "playstation vita"]),
    ("psp 3000", ["psp 3000", "psp-3000"]),
    ("psp 2000", ["psp 2000", "psp-2000"]),
    ("psp", ["psp"]),
    ("xbox series x", ["xbox series x"]),
    ("xbox series s", ["xbox series s"]),
    ("xbox one x", ["xbox one x"]),
    ("xbox one s", ["xbox one s"]),
    ("xbox one", ["xbox one"]),
    ("game boy advance sp", ["game boy advance sp", "gba sp"]),
]

PLATEFORMES = {
    "switch": [
        "nintendo switch", "switch oled", "switch lite", "switch"
    ],
    "ps5": [
        "playstation 5", "ps5"
    ],
    "ps4": [
        "playstation 4", "ps4"
    ],
    "3ds": [
        "nintendo 3ds", "3ds"
    ],
    "ds": [
        "nintendo ds", "ds lite", "dsi"
    ],
    "wiiu": [
        "wii u", "wiiu"
    ],
    "wii": [
        "nintendo wii", "wii"
    ],
    "xbox": [
        "xbox series", "xbox one", "xbox"
    ],
}

PLATEFORMES_INCOMPATIBLES = {
    "switch": ["wii u", "wii", "3ds", "2ds", "nintendo ds", "ps5", "ps4", "xbox"],
    "ps5": ["ps4", "switch", "wii", "3ds", "xbox"],
    "ps4": ["ps5", "switch", "wii", "3ds", "xbox"],
    "3ds": ["switch", "wii u", "wii", "ps5", "ps4", "xbox"],
    "ds": ["switch", "3ds", "wii", "ps5", "ps4", "xbox"],
}

PREUVES_JEU = [
    "jeu", "game", "juego", "gioco", "spiel",
    "nintendo switch", "ps5", "playstation 5", "ps4", "playstation 4",
    "cartouche", "cartridge", "disque", "disc", "blu-ray", "bluray",
    "pegi", "pal",
]


def commence_par(text, termes):
    t = n(text).lstrip(" -|:/[]()")
    for terme in termes:
        q = n(terme)
        if not q:
            continue
        if t == q:
            return True
        for sep in (" ", ":", "-", "|", "/", ","):
            if t.startswith(q + sep):
                return True
    return False


def aliases_console_depuis_texte(text):
    t = n(text)
    for canonique, aliases in CONSOLES:
        if any(present(t, a) for a in aliases):
            return canonique, aliases
    return "", []


def titre_mene_par_produit(title):
    _, aliases = aliases_console_depuis_texte(title)
    if aliases and commence_par(title, aliases):
        return True

    prefixes = [
        "console nintendo", "console switch", "console ps5", "console ps4",
        "console xbox", "console psp", "console ps vita",
        "ti-84", "ti 84", "ti-83", "ti 83", "ti nspire",
        "sony a6000", "canon g7x", "sony walkman", "beelink", "minisforum",
    ]
    return commence_par(title, prefixes)


def plateforme_depuis_texte(title, text):
    titre_n = n(title)
    full_n = n(f"{title} {text}")

    # Le titre est prioritaire.
    for plateforme, termes in PLATEFORMES.items():
        if any(present(titre_n, x) for x in termes):
            return plateforme

    for plateforme, termes in PLATEFORMES.items():
        if any(present(full_n, x) for x in termes):
            return plateforme

    return ""


def type_et_categorie_exemple(title, text, categorie_secours="JEU_EXEMPLE"):
    canonique, aliases = aliases_console_depuis_texte(title)
    titre_n = n(title)

    if aliases and commence_par(title, aliases):
        if not hits(titre_n, PIECES_CONSOLE):
            # Un titre court du type "DS Lite rouge" est bien une console.
            return "console", "CONSOLE", canonique, ""

    plateforme = plateforme_depuis_texte(title, text)

    if plateforme == "switch":
        return "jeu", "JEU_SWITCH", "", "switch"
    if plateforme == "ps5":
        return "jeu", "JEU_PS5", "", "ps5"
    if plateforme == "ps4":
        return "jeu", "JEU_PS4", "", "ps4"
    if plateforme == "3ds":
        return "jeu", "JEU_3DS", "", "3ds"
    if plateforme == "ds":
        return "jeu", "JEU_DS", "", "ds"

    # Dernier recours : ancienne déduction, mais jamais "Nintendo" => Switch
    # pour un titre qui ressemble clairement à une console.
    ancienne = vt.categorie_depuis_exemple(title)
    if ancienne == "JEU_SWITCH" and aliases:
        return "console", "CONSOLE", canonique, ""

    return "jeu", ancienne or categorie_secours, "", plateforme


def mots_coeur(titre, kind, canonique_console=""):
    if kind == "console" and canonique_console:
        mots = re.findall(r"[a-z0-9]+", n(canonique_console))
        mots = [m for m in mots if m not in {"nintendo", "playstation", "xbox"}]
        return list(dict.fromkeys(mots))[:4]

    mots = vt.mots_distinctifs_exemple(titre)
    return list(dict.fromkeys(mots))[:5]


def identite_console_ok(rule, title):
    model = n(rule.get("model", ""))
    canonique, aliases = aliases_console_depuis_texte(model)
    if not aliases:
        return True

    # Pour une règle console, le modèle doit être au début du titre,
    # ou précédé explicitement du mot "console".
    if commence_par(title, aliases):
        return True

    t = n(title).lstrip(" -|:/[]()")
    return any(
        t.startswith("console " + n(a))
        for a in aliases
    )


def categorie_sanity_v69(category, title):
    # Jeux : blocage renforcé des objets dérivés.
    if str(category).startswith("JEU_"):
        mauvais = hits(title, DERIVES_JEU)
        if mauvais:
            return False, "objet dérivé/accessoire: " + ", ".join(mauvais[:3])
        return _ancien_category_sanity(category, title)

    if category == "CONSOLE":
        mauvais_pieces = hits(title, PIECES_CONSOLE)
        if mauvais_pieces:
            return False, "pièce/accessoire console: " + ", ".join(mauvais_pieces[:3])

        access = hits(title, vt.CONSOLE_ACCESSORY_WORDS)
        if access and commence_par(title, access):
            return False, "accessoire console: " + ", ".join(access[:3])

        jeux = hits(title, vt.CONSOLE_GAME_WORDS)
        if jeux and not titre_mene_par_produit(title):
            return False, "annonce de jeu, pas console: " + ", ".join(jeux[:3])

        # Un vrai bundle peut contenir chargeur, câble, jeu, dock, etc.
        return True, ""

    return _ancien_category_sanity(category, title)


def blacklist_check_v69(title, text, blacklist):
    title_hits = vt.title_keyword_hits(
        title,
        blacklist.get("title_accessory_blacklist", []),
    )

    # Si l'accessoire est le sujet du titre => rejet.
    if title_hits and commence_par(title, title_hits):
        return True, "title_accessory_blacklist", title_hits[:3], []

    # Pour un vrai produit placé au début, ne pas rejeter juste parce que
    # "chargeur", "câble", "housse", etc. sont inclus dans le lot.
    produit_mene = titre_mene_par_produit(title)

    if title_hits and not produit_mene:
        # Les mots très forts restent bloquants partout.
        forts = [
            x for x in title_hits
            if n(x) not in {n(y) for y in ACCESSOIRES_BUNDLE_OK}
        ]
        if forts:
            return True, "title_accessory_blacklist", forts[:3], []

    combined = f"{title} {text}"

    # Toujours bloquer les problèmes sérieux.
    for group in ("hard_blacklist", "fake_blacklist"):
        group_hits = vt.keyword_hits(combined, blacklist.get(group, []))
        if group_hits:
            return True, group, group_hits[:3], []

    access_hits = vt.keyword_hits(
        combined,
        blacklist.get("accessory_blacklist", []),
    )
    if access_hits:
        if produit_mene:
            access_hits = [
                x for x in access_hits
                if n(x) not in {n(y) for y in ACCESSOIRES_BUNDLE_OK}
            ]
        if access_hits:
            return True, "accessory_blacklist", access_hits[:3], []

    risks = vt.keyword_hits(
        combined,
        blacklist.get("suspicious_words", []),
    )
    return False, "", [], risks[:3]


def plateforme_ok(rule, title, text, deep):
    expected = rule.get("_plateforme_apprise", "")
    if not expected:
        return True

    full = f"{title} {text}"
    attendu = PLATEFORMES.get(expected, [])

    # Si une mauvaise plateforme est explicitement indiquée sans la bonne,
    # c'est une autre version du jeu.
    mauvais = PLATEFORMES_INCOMPATIBLES.get(expected, [])
    mauvais_hits = hits(full if deep else title, mauvais)
    attendu_present = bool(hits(full if deep else title, attendu))

    if mauvais_hits and not attendu_present:
        return False

    # En vérification profonde, la page doit donner une preuve de plateforme.
    if deep and not attendu_present:
        return False

    return True


def semantique_exemple_ok(rule, title, text, deep=False):
    if not rule.get("_appris_detail"):
        return True

    kind = rule.get("_type_produit", "")
    title_n = n(title)
    full = f"{title} {text}"

    if kind == "jeu":
        if hits(title_n, DERIVES_JEU):
            return False

        if not plateforme_ok(rule, title, text, deep):
            return False

        if deep and not hits(full, PREUVES_JEU):
            return False

    elif kind == "console":
        if hits(title_n, PIECES_CONSOLE):
            return False

        aliases = rule.get("_aliases_console", [])
        if aliases:
            if not commence_par(title, aliases):
                t = n(title).lstrip(" -|:/[]()")
                if not any(t.startswith("console " + n(a)) for a in aliases):
                    return False

    return True


def rule_match_v69(rule, title, text, deep=False):
    if not _ancien_rule_match(rule, title, text, deep=deep):
        return False

    # Un profil permanent ne doit pas seulement partager deux mots :
    # il doit ressembler au type de produit réellement appris.
    pid = rule.get("_profil_id")
    if pid:
        profil = charger_base().get("profils", {}).get(pid)
        if profil:
            score = score_ressemblance_profil(
                profil, title, text, price=None, deep=deep
            )
            minimum = 0.52 if deep else 0.45
            if score < minimum:
                return False

    # Renforce aussi les règles console statiques (3DS, DS Lite, Switch...).
    if not rule.get("_appris_detail"):
        model = rule.get("model", "")
        canonique, aliases = aliases_console_depuis_texte(model)
        if aliases:
            # Ne s'applique que si le modèle ressemble réellement à une console.
            if not identite_console_ok(rule, title):
                return False

    return semantique_exemple_ok(rule, title, text, deep=deep)


async def enrichir_exemple(page, search):
    if not search.get("_exemple_appris"):
        return

    rules = search.get("rules", [])
    if not rules:
        return

    rule = rules[0]
    url = rule.get("source_exemple", "")
    if not url or rule.get("_appris_detail"):
        return

    titre_secours = vt.titre_depuis_lien_exemple(url)
    detail = await vt.verify_listing(page, url, titre_secours)

    if not detail.get("ok"):
        print(
            f"  ? EXEMPLE NON OUVERT | {titre_secours[:60]} | "
            f"on garde la recherche de secours"
        )
        return

    titre = (detail.get("title") or titre_secours).strip()
    texte = detail.get("text") or ""
    prix = detail.get("price")

    kind, categorie, canonique_console, plateforme = type_et_categorie_exemple(
        titre,
        texte,
        search.get("category", "JEU_EXEMPLE"),
    )

    coeur = mots_coeur(titre, kind, canonique_console)
    if not coeur:
        coeur = vt.mots_distinctifs_exemple(titre)

    # Vraie identité apprise depuis la page de l'annonce.
    rule["label"] = f"Appris détail : {titre}"
    rule["model"] = canonique_console or titre
    rule["must_contain"] = coeur[:2] if len(coeur) >= 2 else coeur
    rule["any_contain"] = coeur[2:5]
    rule["exclude"] = list(dict.fromkeys(
        list(rule.get("exclude", []))
        + (PIECES_CONSOLE if kind == "console" else DERIVES_JEU)
    ))
    rule["_appris_detail"] = True
    rule["_type_produit"] = kind
    rule["_plateforme_apprise"] = plateforme
    rule["_titre_exemple"] = titre
    rule["_description_exemple"] = texte[:1800]
    rule["_prix_exemple"] = float(prix) if prix else None

    if kind == "console":
        _, aliases = aliases_console_depuis_texte(canonique_console or titre)
        rule["_aliases_console"] = aliases
        rule["hardware_any"] = []
        rule["platform_any"] = []
        rule["min_margin"] = max(float(rule.get("min_margin", 8)), 12.0)
        rule["min_roi_pct"] = max(float(rule.get("min_roi_pct", 30)), 30.0)
    else:
        if plateforme:
            rule["platform_any"] = PLATEFORMES.get(plateforme, [])
        rule["min_margin"] = max(float(rule.get("min_margin", 8)), 7.0)
        rule["min_roi_pct"] = max(float(rule.get("min_roi_pct", 30)), 28.0)

    search["category"] = categorie

    # Recherche construite depuis le VRAI titre.
    if kind == "console" and canonique_console:
        base_query = canonique_console
    else:
        base_query = vt.recherche_depuis_exemple(titre)

    if kind == "jeu" and plateforme in {"switch", "ps5", "ps4"}:
        mot_plateforme = {
            "switch": "switch",
            "ps5": "ps5",
            "ps4": "ps4",
        }[plateforme]
        if not present(base_query, mot_plateforme):
            base_query = f"{base_query} {mot_plateforme}".strip()

    if base_query:
        search["query"] = base_query

    # Le prix de l'annonce exemple devient une référence de "prix pépite".
    # On laisse une petite tolérance pour trouver une annonce similaire un peu
    # plus chère, sans considérer ce prix comme la valeur de revente.
    if prix and prix > 0:
        facteur = 1.30 if kind == "jeu" else 1.25
        plafond = max(float(prix) * facteur, float(prix) + 4.0)
        search["price_to"] = round(plafond, 2)
        rule["_prix_achat_exemple_max"] = round(plafond, 2)

    profil = sauvegarder_profil_exemple(
        search, rule, url, titre, texte, prix
    )

    print(
        f"  + EXEMPLE ANALYSE + MEMORISE | {titre[:55]} | "
        f"type={kind} | plateforme={plateforme or '-'} | "
        f"prix={prix if prix is not None else '?'} EUR | "
        f"profil={profil.get('profil_id','')}"
    )


def calibrer_regles_exemple_v69(search, cards, blacklist):
    category = search.get("category", "")

    for rule in search.get("rules", []):
        if not rule.get("auto_market"):
            continue

        prix = []

        for c in cards:
            titre = c.get("title", "")
            contenu = c.get("text", "")

            if not rule_match_v69(rule, titre, contenu, deep=False):
                continue

            sane, _ = categorie_sanity_v69(category, titre)
            if not sane:
                continue

            emballage, _ = vt.empty_packaging_check(category, titre, contenu)
            if emballage:
                continue

            bloque, _, _, _ = blacklist_check_v69(
                titre, contenu, blacklist
            )
            if bloque:
                continue

            p = vt.parse_price(contenu)
            if p is None or p <= 2 or p > 350:
                continue

            # Si le titre indique clairement une mauvaise plateforme, on l'écarte.
            if not plateforme_ok(rule, titre, contenu, deep=False):
                continue

            prix.append(float(p))

        if len(prix) < 4:
            rule["resale_low"] = None
            rule["resale_high"] = None
            print(
                f"  ? APPRENTISSAGE DETAILLE | "
                f"{rule.get('model', '')[:50]} | "
                f"{len(prix)} comparable(s) valides"
            )
            continue

        prix.sort()

        # Retire davantage d'extrêmes pour éviter les accessoires et annonces
        # absurdes encore passées dans les résultats.
        if len(prix) >= 10:
            coupe = max(1, int(len(prix) * 0.10))
            prix = prix[coupe:-coupe]

        bas = vt.percentile_simple(prix, 0.40)
        haut = vt.percentile_simple(prix, 0.65)

        if bas is None:
            rule["resale_low"] = None
            rule["resale_high"] = None
            continue

        rule["resale_low"] = round(max(5.0, bas), 2)
        rule["resale_high"] = round(max(rule["resale_low"], haut or bas), 2)

        # Mémorise le marché nettoyé dans le profil permanent.
        pid = rule.get("_profil_id")
        if pid:
            base = charger_base()
            profil = base.get("profils", {}).get(pid)
            if profil:
                profil["prix_marche_bas"] = rule["resale_low"]
                profil["prix_marche_haut"] = rule["resale_high"]
                profil["mis_a_jour"] = datetime.now().isoformat(timespec="seconds")
                base["profils"][pid] = profil
                sauver_base(base)

        # Un exemple est un prix d'achat intéressant, pas une valeur de marché.
        # On limite l'achat à la fois par le marché et par le prix de l'exemple.
        prix_exemple_max = (
            rule.get("_prix_cible_max")
            or rule.get("_prix_achat_exemple_max")
        )
        if prix_exemple_max:
            market_cap = rule["resale_low"] * float(rule.get("max_buy_ratio", 0.55))
            search["price_to"] = round(min(float(prix_exemple_max), market_cap), 2)

        print(
            f"  + MARCHE NETTOYE | {rule.get('model', '')[:48]} | "
            f"{len(prix)} comparables | "
            f"{rule['resale_low']:.2f}-{rule['resale_high']:.2f} EUR"
        )


async def scan_search_v69(page, search, cfg, blacklist, seen_ids):
    if search.get("_exemple_appris"):
        await enrichir_exemple(page, search)

    return await _ancien_scan_search(
        page,
        search,
        cfg,
        blacklist,
        seen_ids,
    )


# Sauvegarde des fonctions V6.8.
_ancien_blacklist_check = vt.blacklist_check
_ancien_category_sanity = vt.category_sanity_check
_ancien_rule_match = vt.rule_match
_ancien_scan_search = vt.scan_search
_ancien_lire_exemples = vt.lire_exemples
_ancien_verify_listing = vt.verify_listing
_ancien_append_alert = vt.append_alert

# Transforme les anciennes alertes en HISTORIQUE uniquement.
# Elles ne deviennent jamais automatiquement des exemples positifs.
importer_alertes_csv_existantes()

# Active les correctifs V7.0 dans tout le scanner.
vt.blacklist_check = blacklist_check_v69
vt.category_sanity_check = categorie_sanity_v69
vt.rule_match = rule_match_v69
vt.calibrer_regles_exemple = calibrer_regles_exemple_v69
vt.scan_search = scan_search_v69
vt.appliquer_exemples = appliquer_exemples_v70
vt.verify_listing = verify_listing_v70
vt.append_alert = append_alert_v70


if __name__ == "__main__":
    print("Vinted V7.0 — mémoire persistante + profils d’achat")
    try:
        asyncio.run(vt.main())
    except KeyboardInterrupt:
        print("\nArret demande.")

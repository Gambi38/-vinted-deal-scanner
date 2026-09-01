#!/usr/bin/env python3
# VERSION : VINTED_V73_PRECISION_DABORD
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
import base64
import io
import math
import urllib.request
import re
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import vinted_tarayici as vt

try:
    import numpy as np
    import onnxruntime as ort
    from PIL import Image, ImageOps
except Exception:
    np = None
    ort = None
    Image = None
    ImageOps = None

# ---------------------------------------------------------------------------
# V7.0 - MEMOIRE PERSISTANTE
# ---------------------------------------------------------------------------
BASE_APPRENTISSAGE = vt.ROOT / "base_apprentissage.json"
EXEMPLES_CLASSES = vt.ROOT / "exemples_classes.txt"
HISTORIQUE_ANNONCES = vt.ROOT / "historique_annonces.jsonl"
REJETS_TXT = vt.ROOT / "rejets.txt"

CACHE_DETAILS = {}
HISTORIQUE_IDS = set()

# Mémoire visuelle : un petit MobileNet ONNX transforme chaque photo en vecteur.
# Le modèle est téléchargé à la demande dans /tmp (environ 14 Mo) et n'est pas
# ajouté au dépôt GitHub. Les vecteurs, eux, sont sauvegardés dans la base.
VISION_MODEL_URL = "https://huggingface.co/onnxmodelzoo/mobilenetv2-7/resolve/main/mobilenetv2-7.onnx?download=true"
VISION_MODEL_PATH = Path("/tmp/vinted_mobilenetv2-7.onnx")
VISION_SESSION = None
VISION_DISABLED_REASON = None
VISION_PAR_TITRE = {}
VISION_IMAGE_CACHE = {}

STOPWORDS_PROFIL = {
    "avec", "pour", "dans", "this", "that", "the", "and", "und", "con",
    "una", "uno", "del", "della", "des", "les", "une", "sur", "vinted",
    "etat", "état", "bon", "bonne", "tres", "très", "comme", "vend",
    "vente", "article", "produit", "neuf", "neuve", "excellent", "condition",
}



# ---------------------------------------------------------------------------
# V7.2 - MEMOIRE VISUELLE
# ---------------------------------------------------------------------------
def _vision_session():
    global VISION_SESSION, VISION_DISABLED_REASON
    if VISION_SESSION is not None:
        return VISION_SESSION
    if VISION_DISABLED_REASON:
        return None
    if np is None or ort is None or Image is None:
        VISION_DISABLED_REASON = "dependances vision absentes"
        return None
    try:
        if not VISION_MODEL_PATH.exists() or VISION_MODEL_PATH.stat().st_size < 5_000_000:
            req = urllib.request.Request(
                VISION_MODEL_URL,
                headers={"User-Agent": "Mozilla/5.0 VintedTarayici/7.2"},
            )
            with urllib.request.urlopen(req, timeout=25) as r, VISION_MODEL_PATH.open("wb") as f:
                while True:
                    bloc = r.read(1024 * 1024)
                    if not bloc:
                        break
                    f.write(bloc)
        VISION_SESSION = ort.InferenceSession(
            str(VISION_MODEL_PATH), providers=["CPUExecutionProvider"]
        )
        print("[INFO] Vision V7.2 activee (MobileNet ONNX).")
        return VISION_SESSION
    except Exception as e:
        VISION_DISABLED_REASON = str(e)[:160]
        print(f"[INFO] Vision indisponible: {VISION_DISABLED_REASON}")
        return None


def _telecharger_image(image_url):
    if not image_url or Image is None:
        return None
    if image_url in VISION_IMAGE_CACHE:
        return VISION_IMAGE_CACHE[image_url]
    try:
        req = urllib.request.Request(
            image_url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "image/avif,image/webp,image/*,*/*"},
        )
        with urllib.request.urlopen(req, timeout=9) as r:
            data = r.read(8 * 1024 * 1024)
        im = Image.open(io.BytesIO(data)).convert("RGB")
        VISION_IMAGE_CACHE[image_url] = im.copy()
        return im
    except Exception:
        return None


def _hash_visuel(im):
    try:
        gris = ImageOps.grayscale(im)
        a = gris.resize((8, 8))
        px = list(a.getdata())
        moy = sum(px) / max(1, len(px))
        ah = 0
        for v in px:
            ah = (ah << 1) | int(v >= moy)

        d = gris.resize((9, 8))
        px2 = list(d.getdata())
        dh = 0
        for y in range(8):
            row = px2[y * 9:(y + 1) * 9]
            for x in range(8):
                dh = (dh << 1) | int(row[x] >= row[x + 1])
        return f"{ah:016x}", f"{dh:016x}"
    except Exception:
        return "", ""


def _encoder_vecteur(vec):
    try:
        arr = np.asarray(vec, dtype=np.float32).reshape(-1)
        norme = float(np.linalg.norm(arr))
        if norme <= 1e-8:
            return ""
        arr = arr / norme
        return base64.b64encode(arr.astype(np.float16).tobytes()).decode("ascii")
    except Exception:
        return ""


def _decoder_vecteur(blob):
    if not blob or np is None:
        return None
    try:
        arr = np.frombuffer(base64.b64decode(blob), dtype=np.float16).astype(np.float32)
        nrm = float(np.linalg.norm(arr))
        if nrm <= 1e-8:
            return None
        return arr / nrm
    except Exception:
        return None


def _empreinte_visuelle(image_url):
    if not image_url:
        return None
    im = _telecharger_image(image_url)
    if im is None:
        return None
    ah, dh = _hash_visuel(im)
    sess = _vision_session()
    embedding = ""
    if sess is not None:
        try:
            # MobileNet v2: image RGB 224x224, normalisation ImageNet.
            im2 = ImageOps.fit(im.convert("RGB"), (224, 224), method=Image.Resampling.BILINEAR)
            arr = np.asarray(im2, dtype=np.float32) / 255.0
            mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
            std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
            arr = (arr - mean) / std
            arr = np.transpose(arr, (2, 0, 1))[None, ...]
            inp = sess.get_inputs()[0].name
            out = sess.run(None, {inp: arr})[0]
            embedding = _encoder_vecteur(out)
        except Exception:
            embedding = ""
    return {
        "image_url": image_url,
        "embedding": embedding,
        "ahash": ah,
        "dhash": dh,
    }


def _hamming_hex(a, b):
    if not a or not b:
        return None
    try:
        x = int(a, 16) ^ int(b, 16)
        return x.bit_count() / max(1, len(a) * 4)
    except Exception:
        return None


def similarite_visuelle(emp_a, emp_b):
    if not emp_a or not emp_b:
        return 0.0
    scores = []
    va = _decoder_vecteur(emp_a.get("embedding"))
    vb = _decoder_vecteur(emp_b.get("embedding"))
    if va is not None and vb is not None and len(va) == len(vb):
        try:
            cos = float(np.dot(va, vb))
            # ramène [-1,1] vers [0,1], en privilégiant la zone utile.
            scores.append(max(0.0, min(1.0, (cos + 1.0) / 2.0)))
        except Exception:
            pass
    for cle in ("ahash", "dhash"):
        d = _hamming_hex(emp_a.get(cle), emp_b.get(cle))
        if d is not None:
            scores.append(max(0.0, 1.0 - d))
    if not scores:
        return 0.0
    # Le réseau compte davantage que le hash si disponible.
    if len(scores) >= 3:
        return round(scores[0] * 0.72 + scores[1] * 0.14 + scores[2] * 0.14, 3)
    return round(sum(scores) / len(scores), 3)


def _empreinte_candidate(titre):
    return VISION_PAR_TITRE.get(n(titre))


def _meilleure_similarite(emp, profils):
    meilleur = (0.0, None)
    if not emp:
        return meilleur
    for pid, profil in profils.items():
        for ref in profil.get("empreintes_visuelles", [])[-8:]:
            s = similarite_visuelle(emp, ref)
            if s > meilleur[0]:
                meilleur = (s, pid)
    return meilleur


def scores_vision_candidat(titre):
    emp = _empreinte_candidate(titre)
    if not emp:
        return {"positif": 0.0, "positif_id": None, "negatif": 0.0, "negatif_id": None}
    base = charger_base()
    pos_s, pos_id = _meilleure_similarite(emp, base.get("profils", {}))
    neg_s, neg_id = _meilleure_similarite(emp, base.get("profils_negatifs", {}))
    return {"positif": pos_s, "positif_id": pos_id, "negatif": neg_s, "negatif_id": neg_id}


def ajouter_empreinte_profil(profil, emp, image_url):
    if not emp:
        return profil
    refs = list(profil.get("empreintes_visuelles", []))
    # évite doublon d'URL / d'empreinte exacte
    if not any(r.get("image_url") == image_url or (r.get("dhash") and r.get("dhash") == emp.get("dhash")) for r in refs):
        refs.append(emp)
    profil["empreintes_visuelles"] = refs[-8:]
    profil["image_reference_url"] = image_url or profil.get("image_reference_url", "")
    return profil


def memoriser_vision_profil(pid, titre, image_url, negatif=False):
    emp = _empreinte_candidate(titre) or _empreinte_visuelle(image_url)
    if not emp:
        return False
    base = charger_base()
    cle = "profils_negatifs" if negatif else "profils"
    profil = base.get(cle, {}).get(pid)
    if not profil:
        return False
    ajouter_empreinte_profil(profil, emp, image_url)
    profil["mis_a_jour"] = datetime.now().isoformat(timespec="seconds")
    base[cle][pid] = profil
    sauver_base(base)
    return True


def charger_base():
    if not BASE_APPRENTISSAGE.exists():
        return {
            "version": 1,
            "profils": {},
            "profils_negatifs": {},
            "liens_classes": [],
            "liens_rejetes_classes": [],
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
    data.setdefault("profils_negatifs", {})
    data.setdefault("liens_classes", [])
    data.setdefault("liens_rejetes_classes", [])
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
            f"{profil.get('prix_cible_median') if profil.get('prix_cible_median') is not None else '?'} EUR | "
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
        "empreintes_visuelles": list(ancien.get("empreintes_visuelles", []))[-8:],
        "image_reference_url": ancien.get("image_reference_url", ""),
        "mis_a_jour": datetime.now().isoformat(timespec="seconds"),
    }

    emp = _empreinte_candidate(titre)
    if emp:
        ajouter_empreinte_profil(profil, emp, emp.get("image_url", ""))
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
        "max_buy_ratio": 0.65 if profil.get("type_produit") == "jeu" else 0.60,
        "min_margin": 8 if profil.get("type_produit") == "jeu" else 15,
        "min_roi_pct": 18 if profil.get("type_produit") == "jeu" else 20,
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
    # V7.3 : on élargit légèrement le prix acceptable, mais on renforce
    # l'identité produit. Objectif : moins de "fausses pépites", davantage
    # de vrais articles même s'ils coûtent un peu plus cher.
    appliquer_mode_precision(cfg)

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

    nb_vision = ajouter_patrouilles_vision(cfg)
    if nb_vision:
        print(f"[INFO] {nb_vision} patrouille(s) visuelle(s) ajoutée(s) pour les annonces mal décrites.")

    nb_rattrapage = ajouter_rattrapage_profils_incomplets(cfg)
    if nb_rattrapage:
        print(f"[INFO] {nb_rattrapage} ancien(s) profil(s) a completer (prix/photo).")

    nb_rejets = ajouter_rejets_aux_recherches(cfg)
    if nb_rejets:
        print(f"[INFO] {nb_rejets} faux positif(s) à apprendre depuis rejets.txt.")

    return len(recherches)



def lire_rejets():
    """Liens donnés par l'utilisateur comme faux positifs à ne pas acheter."""
    if not REJETS_TXT.exists():
        return []
    urls = []
    try:
        for ligne in REJETS_TXT.read_text(encoding="utf-8").splitlines():
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#"):
                continue
            if "vinted." not in ligne or "/items/" not in ligne:
                continue
            url = ligne.split("?")[0]
            if url not in urls:
                urls.append(url)
    except Exception:
        return []
    return urls


def liens_rejetes_deja_classes():
    base = charger_base()
    return set(base.get("liens_rejetes_classes", []))


def tokens_rejet(texte):
    mots = re.findall(r"[a-z0-9]{2,}", n(texte))
    inutiles = STOPWORDS_PROFIL | {
        "nintendo", "switch", "playstation", "ps5", "ps4", "xbox",
        "console", "jeu", "jeux", "game", "games", "vinted",
        "pour", "avec", "sans", "the", "and", "les", "des",
    }
    return [
        x for x in mots
        if x not in inutiles and len(x) >= 2
    ]


def sauvegarder_rejet(url, titre, texte, prix):
    """Mémorise un faux positif sans transformer tout un mot générique en blacklist."""
    base = charger_base()
    profils = base.setdefault("profils_negatifs", {})
    classes = base.setdefault("liens_rejetes_classes", [])

    titre_tokens = list(dict.fromkeys(tokens_rejet(titre)))[:12]
    desc_tokens = mots_signature(texte, titre)[:20]
    rid_source = f"{n(titre)}|{' '.join(titre_tokens[:6])}"
    rid = hashlib.sha1(rid_source.encode("utf-8")).hexdigest()[:14]

    ancien = profils.get(rid, {})
    urls = list(ancien.get("liens", []))
    if url not in urls:
        urls.append(url)

    profils[rid] = {
        "rejet_id": rid,
        "actif": True,
        "titre_reference": titre,
        "tokens_titre": list(dict.fromkeys(
            list(ancien.get("tokens_titre", [])) + titre_tokens
        ))[:16],
        "mots_description": list(dict.fromkeys(
            list(ancien.get("mots_description", [])) + desc_tokens
        ))[:28],
        "prix_reference": round(float(prix), 2) if prix else ancien.get("prix_reference"),
        "liens": urls[-20:],
        "nombre_exemples": len(urls),
        "description_reference": (texte or "")[:1400],
        "empreintes_visuelles": list(ancien.get("empreintes_visuelles", []))[-8:],
        "image_reference_url": ancien.get("image_reference_url", ""),
        "mis_a_jour": datetime.now().isoformat(timespec="seconds"),
    }
    emp = _empreinte_candidate(titre)
    if emp:
        ajouter_empreinte_profil(profils[rid], emp, emp.get("image_url", ""))
    if url not in classes:
        classes.append(url)

    sauver_base(base)
    return profils[rid]


def multicart_interdit(titre, texte=""):
    """Détecte les cartouches du type 19-in-1 / 22-en-1 / 99 jeux, R4, multicartes."""
    t = n(f"{titre} {texte[:600]}")
    titre_n = n(titre)

    if re.search(r"\b\d{2,4}\s*(?:in|en)\s*1\b", titre_n):
        return True, "cartouche multi-jeux X-en-1"

    mots_multi = (
        "multicart", "multi cart", "multijeux", "multi jeux",
        "multigame", "multi game", "multijuegos", "multi juegos",
        "flashcard", "flash card", "carte r4", "r4 card",
    )
    if any(x in titre_n for x in mots_multi):
        return True, "cartouche multi-jeux / flashcard"

    a_un_nombre_de_jeux = re.search(
        r"\b\d{2,4}\s*(?:jeux|games|juegos|giochi|jogos)\b",
        titre_n,
    )
    support = any(x in t for x in (
        "cartouche", "cartridge", "tarjeta", "card", "scheda",
        "cartucho", "cartuccia", "r4",
    ))
    if a_un_nombre_de_jeux and support:
        return True, "nombre anormal de jeux sur une cartouche"

    return False, ""


def ressemble_a_rejet_appris(titre, texte="", deep=False):
    dur, raison = multicart_interdit(titre, texte)
    if dur:
        return True, raison

    cand = set(tokens_rejet(titre))
    if not cand:
        return False, ""

    base = charger_base()
    for profil in base.get("profils_negatifs", {}).values():
        if not profil.get("actif", True):
            continue

        ref = set(profil.get("tokens_titre", []))
        if not ref:
            continue

        inter = cand & ref
        union = cand | ref
        jaccard = len(inter) / max(1, len(union))

        # Très proche du faux positif fourni par l'utilisateur.
        if len(inter) >= 2 and jaccard >= 0.72:
            return True, f"ressemble à un rejet appris ({jaccard:.0%})"

        # Avec la description complète, on peut reconnaître une variante
        # dont le titre a légèrement changé.
        if deep and len(inter) >= 2 and jaccard >= 0.50:
            desc = profil.get("mots_description", [])[:14]
            full_n = n(f"{titre} {texte}")
            desc_hits = sum(1 for x in desc if present(full_n, x))
            if desc and desc_hits >= min(4, max(2, len(desc) // 3)):
                return True, "titre + description proches d'un rejet appris"

    return False, ""


async def enrichir_rejet(page, search):
    url = str(search.get("_source_rejet", "")).split("?")[0]
    if not url:
        return

    if url in liens_rejetes_deja_classes():
        return

    secours = vt.titre_depuis_lien_exemple(url)
    detail = await vt.verify_listing(page, url, secours)
    if not detail.get("ok"):
        print(f"  ? REJET NON OUVERT | {secours[:60]}")
        return

    titre = (detail.get("title") or secours).strip()
    texte = detail.get("text") or ""
    prix = detail.get("price")
    profil = sauvegarder_rejet(url, titre, texte, prix)

    print(
        f"  - FAUX POSITIF MEMORISE | {titre[:55]} | "
        f"prix={prix if prix is not None else '?'} EUR | "
        f"rejet={profil.get('rejet_id','')}"
    )


def ajouter_rejets_aux_recherches(cfg):
    classes = liens_rejetes_deja_classes()
    pendants = [
        u for u in lire_rejets()
        if u not in classes
    ][:4]

    if not pendants:
        return 0

    recherches = []
    for url in pendants:
        recherches.append({
            "name": f"REJET - {vt.titre_depuis_lien_exemple(url)}",
            "category": "REJET",
            "query": "",
            "price_to": None,
            "max_items": 0,
            "rules": [],
            "_rejet_appris": True,
            "_source_rejet": url,
            "_priorite_personnelle": True,
        })

    cfg["searches"] = recherches + list(cfg.get("searches", []))
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


async def _prix_vinted_fallback(page, url):
    """Deuxième lecture uniquement si Vinted n'expose plus l'ancien meta price."""
    probe = None
    try:
        probe = await page.context.new_page()
        await probe.goto(
            url,
            wait_until="domcontentloaded",
            timeout=9000,
        )
        await probe.wait_for_timeout(550)

        # 1) JSON-LD / schema.org : source la plus propre.
        try:
            scripts = await probe.locator(
                'script[type="application/ld+json"]'
            ).all_text_contents()

            def prix_offre(obj):
                if isinstance(obj, dict):
                    typ = str(obj.get("@type", "")).lower()
                    if typ in {"offer", "aggregateoffer"}:
                        for cle in ("price", "lowPrice", "highPrice"):
                            val = obj.get(cle)
                            if isinstance(val, (int, float)) and 0 < float(val) < 5000:
                                return float(val)
                            if isinstance(val, str):
                                m = re.search(r"\d+(?:[.,]\d{1,2})?", val)
                                if m:
                                    return float(m.group(0).replace(",", "."))
                    if "offers" in obj:
                        x = prix_offre(obj.get("offers"))
                        if x:
                            return x
                    for val in obj.values():
                        x = prix_offre(val)
                        if x:
                            return x
                elif isinstance(obj, list):
                    for val in obj:
                        x = prix_offre(val)
                        if x:
                            return x
                return None

            for brut in scripts:
                try:
                    data = json.loads(brut)
                except Exception:
                    continue
                x = prix_offre(data)
                if x:
                    return round(x, 2)
        except Exception:
            pass

        # 2) Métadonnées / éléments connus.
        for selector, attr in (
            ('meta[property="product:price:amount"]', "content"),
            ('meta[property="og:price:amount"]', "content"),
            ('meta[itemprop="price"]', "content"),
            ('[itemprop="price"]', "content"),
        ):
            try:
                loc = probe.locator(selector).first
                val = await loc.get_attribute(attr, timeout=700)
                if val:
                    m = re.search(r"\d+(?:[.,]\d{1,2})?", val)
                    if m:
                        x = float(m.group(0).replace(",", "."))
                        if 0 < x < 5000:
                            return round(x, 2)
            except Exception:
                pass

        # 3) Dernier secours : ligne visible "Prix/Price/Precio/Prezzo ... €".
        try:
            body = await probe.locator("body").inner_text(timeout=1600)
            motifs = (
                r"(?:prix|price|precio|prezzo)\s*:?\s*(\d+(?:[.,]\d{1,2})?)\s*€",
                r"(\d+(?:[.,]\d{1,2})?)\s*€",
            )
            for motif in motifs:
                m = re.search(motif, n(body), re.I)
                if m:
                    x = float(m.group(1).replace(",", "."))
                    if 0 < x < 5000:
                        return round(x, 2)
        except Exception:
            pass

    except Exception:
        pass
    finally:
        if probe is not None:
            try:
                await probe.close()
            except Exception:
                pass

    return None


async def verify_listing_v70(page, url, fallback_title=""):
    detail = await _ancien_verify_listing(page, url, fallback_title)

    if detail.get("ok") and not detail.get("price"):
        prix = await _prix_vinted_fallback(page, url)
        if prix:
            detail["price"] = prix

    if detail.get("ok"):
        image_url = detail.get("image_url") or ""
        emp = _empreinte_visuelle(image_url) if image_url else None
        if emp:
            VISION_PAR_TITRE[n(detail.get("title") or fallback_title)] = emp
            detail["vision_disponible"] = True
        else:
            detail["vision_disponible"] = False
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
        "image_url": detail.get("image_url", ""),
        "vision": scores_vision_candidat(detail.get("title") or row.get("title", "")),
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
    vis = scores_vision_candidat(title)
    # Une photo très proche d'un rejet utilisateur bloque, sauf si elle est
    # au moins aussi proche d'un bon exemple. Le seuil élevé évite de confondre
    # deux cartouches/boîtes visuellement proches.
    if vis["negatif"] >= 0.93 and vis["negatif"] > vis["positif"] + 0.035:
        return True, "apprentissage_negatif_image", [f"photo rejet {vis['negatif']:.2f}"], []

    negatif, raison_negative = ressemble_a_rejet_appris(
        title,
        text,
        deep=bool(text and len(str(text)) > 120),
    )
    if negatif:
        return True, "apprentissage_negatif", [raison_negative], []

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


def vision_score_pour_profil(rule, title):
    pid = rule.get("_profil_id")
    emp = _empreinte_candidate(title)
    if not pid or not emp:
        return 0.0
    profil = charger_base().get("profils", {}).get(pid, {})
    score = 0.0
    for ref in profil.get("empreintes_visuelles", [])[-8:]:
        score = max(score, similarite_visuelle(emp, ref))
    return score


def semantique_exemple_ok(rule, title, text, deep=False):
    if not rule.get("_appris_detail"):
        return True

    kind = rule.get("_type_produit", "")
    title_n = n(title)
    full = f"{title} {text}"
    vis_pid = vision_score_pour_profil(rule, title) if deep else 0.0

    if kind == "jeu":
        if hits(title_n, DERIVES_JEU):
            return False

        expected = rule.get("_plateforme_apprise", "")
        if expected:
            attendu = PLATEFORMES.get(expected, [])
            mauvais = PLATEFORMES_INCOMPATIBLES.get(expected, [])
            mauvais_hits = hits(full if deep else title, mauvais)
            attendu_present = bool(hits(full if deep else title, attendu))
            # Une plateforme explicitement différente reste bloquante, même si
            # la photo est proche.
            if mauvais_hits and not attendu_present:
                return False
            # Si le vendeur n'a rien écrit sur la plateforme, la photo peut
            # servir de preuve de secours.
            if deep and not attendu_present and vis_pid < 0.90:
                return False

        if deep and not hits(full, PREUVES_JEU) and vis_pid < 0.90:
            return False

    elif kind == "console":
        if hits(title_n, PIECES_CONSOLE):
            return False

        aliases = rule.get("_aliases_console", [])
        if aliases:
            ok_nom = commence_par(title, aliases)
            if not ok_nom:
                t = n(title).lstrip(" -|:/[]()")
                ok_nom = any(t.startswith("console " + n(a)) for a in aliases)
            # Un titre vague comme "console noire" peut passer si la photo
            # ressemble fortement aux bons exemples du même profil.
            if not ok_nom and vis_pid < 0.90:
                return False

    return True


def rule_match_v69(rule, title, text, deep=False):
    ancien_ok = _ancien_rule_match(rule, title, text, deep=deep)

    # Pour une recherche issue de la base, le premier passage est volontairement
    # un peu plus permissif : la requête Vinted a déjà ciblé le produit et cela
    # permet d'ouvrir une annonce mal décrite pour regarder sa photo.
    if not ancien_ok and not (rule.get("_profil_db") and not deep):
        return False

    # Un profil permanent ne doit pas seulement partager deux mots :
    # il doit ressembler au type de produit réellement appris.
    pid = rule.get("_profil_id")
    if pid:
        profil = charger_base().get("profils", {}).get(pid)
        if profil:
            score = score_ressemblance_profil(
                profil, title, text, prix=None, deep=deep
            )
            minimum = 0.58 if deep else 0.43
            if deep:
                emp = _empreinte_candidate(title)
                vis_pid = 0.0
                if emp:
                    for ref in profil.get("empreintes_visuelles", [])[-8:]:
                        vis_pid = max(vis_pid, similarite_visuelle(emp, ref))
                # Une très bonne proximité photo peut sauver un titre médiocre.
                # Elle ne contourne jamais les blacklists fortes ni une plateforme
                # explicitement contradictoire (contrôlées ailleurs).
                if score < minimum and vis_pid < 0.91:
                    return False
            elif score < minimum and ancien_ok:
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
        facteur = 1.55 if kind == "jeu" else 1.40
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
            ratio = float(rule.get("max_buy_ratio", 0.65 if rule.get("_type_produit") == "jeu" else 0.60))
            market_cap = rule["resale_low"] * ratio

            # Un exemple peut être un jackpot exceptionnel. On ne veut donc
            # plus limiter toutes les futures alertes à +25/30 % de ce jackpot.
            # On accepte au moins environ la moitié de la valeur prudente du
            # marché, tout en gardant une marge/ROI minimum.
            plancher_raisonnable = rule["resale_low"] * (
                0.52 if rule.get("_type_produit") == "jeu" else 0.50
            )
            cible = max(float(prix_exemple_max), plancher_raisonnable)
            search["price_to"] = round(min(cible, market_cap), 2)

        print(
            f"  + MARCHE NETTOYE | {rule.get('model', '')[:48]} | "
            f"{len(prix)} comparables | "
            f"{rule['resale_low']:.2f}-{rule['resale_high']:.2f} EUR"
        )



def ajouter_rattrapage_profils_incomplets(cfg):
    """Réouvre quelques anciens exemples si prix/photo n'avaient pas pu être appris."""
    base = charger_base()
    ajouts = []
    for pid, profil in base.get("profils", {}).items():
        if len(ajouts) >= 3:
            break
        manque_prix = not profil.get("prix_cible_max")
        manque_vision = not profil.get("empreintes_visuelles")
        liens = profil.get("liens_exemples", [])
        if (manque_prix or manque_vision) and liens:
            ajouts.append({
                "name": f"RATTRAPAGE - {profil.get('modele','')}",
                "category": "RATTRAPAGE",
                "query": "",
                "price_to": None,
                "max_items": 0,
                "rules": [],
                "_rattrapage_profil": pid,
                "_source_rattrapage": liens[0],
                "_priorite_personnelle": True,
            })
    if ajouts:
        cfg["searches"] = ajouts + list(cfg.get("searches", []))
    return len(ajouts)


async def enrichir_rattrapage(page, search):
    pid = search.get("_rattrapage_profil")
    url = search.get("_source_rattrapage")
    if not pid or not url:
        return
    base = charger_base()
    profil = base.get("profils", {}).get(pid)
    if not profil:
        return
    detail = await vt.verify_listing(page, url, profil.get("titre_reference", ""))
    if not detail.get("ok"):
        return
    prix = detail.get("price")
    titre = detail.get("title") or profil.get("titre_reference", "")
    image_url = detail.get("image_url", "")
    if prix and prix > 0:
        lst = [float(x) for x in profil.get("prix_exemples", []) if isinstance(x, (int,float)) and x > 0]
        p = round(float(prix), 2)
        if p not in lst:
            lst.append(p)
        med = statistics.median(lst)
        plafond = max(min(max(lst) * 1.18, med * 1.35), med + 3.0)
        profil["prix_exemples"] = sorted(lst)
        profil["prix_cible_median"] = round(med, 2)
        profil["prix_cible_max"] = round(plafond, 2)
    emp = _empreinte_candidate(titre)
    if emp:
        ajouter_empreinte_profil(profil, emp, image_url)
    profil["mis_a_jour"] = datetime.now().isoformat(timespec="seconds")
    base["profils"][pid] = profil
    sauver_base(base)
    print(f"  + RATTRAPAGE PROFIL | {titre[:55]} | prix={prix if prix is not None else '?'} | vision={'oui' if emp else 'non'}")



# ---------------------------------------------------------------------------
# V7.3 - PRECISION D'ABORD
# ---------------------------------------------------------------------------
def appliquer_mode_precision(cfg):
    """
    Elargit modérément les prix observés, mais conserve des exigences fortes
    sur l'identité du produit. Le but n'est plus "le moins cher possible",
    mais "un vrai produit rentable avec forte confiance".
    """
    for search in cfg.get("searches", []):
        if not isinstance(search, dict):
            continue
        if any(search.get(k) for k in (
            "_exemple_appris", "_profil_db", "_rejet_appris",
            "_rattrapage_profil", "_vision_patrouille"
        )):
            continue

        categorie = str(search.get("category", ""))
        prix_max = search.get("price_to")
        try:
            p = float(prix_max) if prix_max is not None else None
        except Exception:
            p = None

        if p and p > 0:
            if categorie.startswith("JEU_"):
                # +25 %, mais jamais plus de +20 EUR d'un coup.
                search["price_to"] = round(min(p * 1.25, p + 20.0), 2)
            elif categorie == "CONSOLE":
                # +20 %, plafond d'élargissement +35 EUR.
                search["price_to"] = round(min(p * 1.20, p + 35.0), 2)
            elif categorie == "ELECTRONIQUE":
                search["price_to"] = round(min(p * 1.18, p + 40.0), 2)

        for rule in search.get("rules", []):
            if not isinstance(rule, dict):
                continue
            try:
                actuel_ratio = float(rule.get("max_buy_ratio", 0.40))
            except Exception:
                actuel_ratio = 0.40

            if categorie.startswith("JEU_"):
                rule["max_buy_ratio"] = max(actuel_ratio, 0.60)
                try:
                    rule["min_margin"] = min(float(rule.get("min_margin", 10)), 8.0)
                except Exception:
                    rule["min_margin"] = 8.0
                try:
                    rule["min_roi_pct"] = min(float(rule.get("min_roi_pct", 30)), 20.0)
                except Exception:
                    rule["min_roi_pct"] = 20.0

            elif categorie == "CONSOLE":
                rule["max_buy_ratio"] = max(actuel_ratio, 0.58)
                try:
                    rule["min_margin"] = min(float(rule.get("min_margin", 20)), 15.0)
                except Exception:
                    rule["min_margin"] = 15.0
                try:
                    rule["min_roi_pct"] = min(float(rule.get("min_roi_pct", 30)), 20.0)
                except Exception:
                    rule["min_roi_pct"] = 20.0

            elif categorie == "ELECTRONIQUE":
                rule["max_buy_ratio"] = max(actuel_ratio, 0.56)
                try:
                    rule["min_margin"] = min(float(rule.get("min_margin", 20)), 15.0)
                except Exception:
                    rule["min_margin"] = 15.0
                try:
                    rule["min_roi_pct"] = min(float(rule.get("min_roi_pct", 30)), 20.0)
                except Exception:
                    rule["min_roi_pct"] = 20.0


def _groupe_vision_profil(profil):
    plateforme = n(profil.get("plateforme", ""))
    modele = n(profil.get("modele", ""))
    type_produit = profil.get("type_produit", "")

    if plateforme == "switch" or "switch" in modele:
        if type_produit == "console":
            return ("switch_console", "nintendo switch", 170.0)
        return ("switch_jeu", "nintendo switch", 55.0)
    if plateforme == "ps5" or "ps5" in modele or "playstation 5" in modele:
        if type_produit == "console":
            return ("ps5_console", "ps5", 230.0)
        return ("ps5_jeu", "ps5", 65.0)
    if plateforme == "ps4" or "ps4" in modele or "playstation 4" in modele:
        if type_produit == "console":
            return ("ps4_console", "ps4", 110.0)
        return ("ps4_jeu", "ps4", 45.0)
    if "3ds" in modele:
        return ("3ds", "nintendo 3ds", 95.0)
    if "ds lite" in modele or ("nintendo ds" in modele and type_produit == "console"):
        return ("ds", "nintendo ds", 60.0)
    if "vita" in modele:
        return ("vita", "ps vita", 80.0)
    if "psp" in modele:
        return ("psp", "psp", 55.0)
    return None


def ajouter_patrouilles_vision(cfg):
    """
    Recherche large et récente. Elle sert uniquement à retrouver un bon produit
    dont le vendeur a écrit un titre pauvre ("jeu switch", "console noire", etc.).
    Maximum deux patrouilles par run pour ne pas ralentir le scanner.
    """
    base = charger_base()
    groupes = {}
    for profil in base.get("profils", {}).values():
        if not profil.get("actif", True):
            continue
        if not profil.get("empreintes_visuelles"):
            continue
        g = _groupe_vision_profil(profil)
        if not g:
            continue
        cle, query, prix = g
        groupes.setdefault(cle, {"query": query, "price_to": prix, "profils": []})
        groupes[cle]["profils"].append(profil.get("profil_id"))

    if not groupes:
        return 0

    cles = sorted(groupes)
    # Rotation déterministe toutes les 5 minutes.
    tranche = int(datetime.now().timestamp() // 300)
    depart = tranche % len(cles)
    selection = [cles[(depart + i) % len(cles)] for i in range(min(2, len(cles)))]

    recherches = []
    for cle in selection:
        g = groupes[cle]
        recherches.append({
            "name": f"VISION - chasse {cle}",
            "category": "VISION",
            "query": g["query"],
            "price_to": g["price_to"],
            "max_items": 14,
            "_vision_patrouille": True,
            "_vision_profils": g["profils"],
            "_priorite_personnelle": True,
        })

    if recherches:
        cfg["searches"] = recherches + list(cfg.get("searches", []))
    return len(recherches)


def _regle_depuis_profil_pour_vision(profil):
    return recherche_depuis_profil(profil)["rules"][0]


async def scan_patrouille_vision(page, search, cfg, blacklist, seen_ids):
    """
    Passe secondaire orientée image. On ouvre seulement quelques annonces,
    puis on compare leur photo à la mémoire positive ET négative.
    """
    base_url = cfg.get("base_url", "https://www.vinted.be").rstrip("/")
    url = (
        f"{base_url}/catalog?search_text={vt.quote_plus(search.get('query',''))}"
        f"&order=newest_first"
    )
    if search.get("price_to") is not None:
        url += f"&price_to={float(search['price_to']):g}"

    print(f"\n[SCAN] {search['name']} -> {url}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=14000)
        await page.wait_for_timeout(int(cfg.get("page_wait_ms", 900)))
        await page.locator('a[href*="/items/"]').first.wait_for(timeout=4000)
        cards = await vt.extract_cards(page)
    except Exception as e:
        print(f"  ! VISION PATROUILLE indisponible | {str(e)[:90]}")
        return []

    base = charger_base()
    profils_tous = base.get("profils", {})
    ids_autorises = set(search.get("_vision_profils", []))
    nouvelles = []
    ouvertes = 0

    for c in cards[:int(search.get("max_items", 14))]:
        if ouvertes >= 6:
            break
        if c.get("item_id") in seen_ids:
            continue

        titre_carte = c.get("title", "")
        texte_carte = c.get("text", "")
        prix_carte = vt.parse_price(texte_carte)
        if prix_carte is None or prix_carte <= 1:
            continue

        # Avant d'ouvrir la page : élimine uniquement les signaux certains.
        bloque, groupe, hs, _ = vt.blacklist_check(titre_carte, texte_carte, blacklist)
        if bloque:
            continue
        bas_valeur, _ = vt.low_value_game_check(titre_carte, texte_carte, blacklist)
        if bas_valeur:
            continue

        ouvertes += 1
        detail = await vt.verify_listing(page, c.get("url", ""), titre_carte)
        if not detail.get("ok"):
            continue

        titre = detail.get("title") or titre_carte
        texte = detail.get("text") or texte_carte
        prix = detail.get("price") or prix_carte

        # Maintenant que verify_listing a mémorisé l'empreinte de cette photo.
        vis = scores_vision_candidat(titre)
        pid = vis.get("positif_id")
        pos = float(vis.get("positif") or 0.0)
        neg = float(vis.get("negatif") or 0.0)

        if not pid or pid not in ids_autorises:
            continue
        if neg >= 0.92 and neg > pos + 0.04:
            print(f"  X VISION REJET | {titre[:58]} | bon={pos:.2f} rejet={neg:.2f}")
            continue

        # Pour une annonce très mal décrite, la photo doit être extrêmement proche.
        # Avec un peu de texte concordant, 0.91 suffit.
        profil = profils_tous.get(pid)
        if not profil:
            continue
        texte_score = score_ressemblance_profil(
            profil, titre, texte, prix=prix, deep=True
        )
        if not (
            (pos >= 0.96)
            or (pos >= 0.91 and texte_score >= 0.30)
        ):
            continue

        rule = _regle_depuis_profil_pour_vision(profil)
        categorie = profil.get("categorie", "")

        # Les contradictions écrites gardent priorité sur la photo.
        if not semantique_exemple_ok(rule, titre, texte, deep=True):
            continue

        bloque, groupe, hs, risques = vt.blacklist_check(titre, texte, blacklist)
        if bloque:
            continue

        packaging, _ = vt.empty_packaging_check(categorie, titre, texte)
        if packaging:
            continue

        condition_ok, mauvais_etat, rare = vt.condition_check(
            f"{titre} {texte}", cfg, rule
        )
        if not condition_ok:
            continue

        elec_ok, _ = vt.electronics_condition_check(
            titre, texte, cfg, categorie
        )
        if not elec_ok:
            continue

        # Pas d'alerte visuelle si on n'a pas encore une valeur de marché solide.
        if not profil.get("prix_marche_bas"):
            continue
        rule["resale_low"] = float(profil["prix_marche_bas"])
        rule["resale_high"] = float(
            profil.get("prix_marche_haut") or profil["prix_marche_bas"]
        )

        if profil.get("type_produit") == "jeu":
            rule["max_buy_ratio"] = 0.65
            rule["min_margin"] = 8.0
            rule["min_roi_pct"] = 18.0
        else:
            rule["max_buy_ratio"] = 0.60
            rule["min_margin"] = 15.0
            rule["min_roi_pct"] = 20.0

        total, resale_low, resale_high, margin_low, margin_high, roi_low = (
            vt.score_candidate(rule, float(prix), cfg)
        )
        if margin_low is None:
            continue

        reference = float(rule.get("market_avg", resale_low))
        if float(prix) > reference * float(rule["max_buy_ratio"]):
            continue
        if margin_low < float(rule["min_margin"]):
            continue
        if roi_low < float(rule["min_roi_pct"]):
            continue

        motivation = vt.keyword_hits(
            f"{titre} {texte}",
            cfg.get("seller_motivation_words", []),
        )
        score = vt.opportunity_score(
            float(prix), reference, margin_low, motivation,
            authenticity_risk=False, rare_condition=False
        )
        # La concordance photo augmente la confiance produit, pas la marge.
        if pos >= 0.96:
            score = min(10, score + 1)

        raison = (
            f"photo proche d'un BON exemple ({pos:.0%}); "
            + vt.reason_text(float(prix), reference, motivation)
        )

        risque_list = list(dict.fromkeys(risques))
        if pos < 0.94:
            risque_list.append("correspondance photo à confirmer")

        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "category": categorie,
            "search": f"{search['name']} / profil visuel {profil.get('modele','')}",
            "brand": "",
            "model": profil.get("modele", ""),
            "size": vt.extract_size(f"{titre} {texte}"),
            "opportunity_score": score,
            "title": titre,
            "image_url": detail.get("image_url", ""),
            "listing_price": round(float(prix), 2),
            "total_buy_est": total,
            "resale_low": resale_low,
            "resale_high": resale_high,
            "margin_low": margin_low,
            "margin_high": margin_high,
            "roi_low": roi_low,
            "demand_score": int(rule.get("demand_score", 5)),
            "risk": ", ".join(risque_list),
            "reason": raison,
            "url": c.get("url", ""),
            "item_id": c.get("item_id", ""),
        }

        vt.append_alert(row)
        nouvelles.append(row)
        vt.ntfy_send(row)
        seen_ids.add(c.get("item_id"))
        print(
            f"  ★ VISION {score}/10 | {titre[:52]} | {float(prix):.2f} EUR | "
            f"photo={pos:.2f} | marge +{margin_low:.2f}"
        )

    return nouvelles


async def scan_search_v69(page, search, cfg, blacklist, seen_ids):
    if search.get("_vision_patrouille"):
        return await scan_patrouille_vision(page, search, cfg, blacklist, seen_ids)

    if search.get("_rattrapage_profil"):
        await enrichir_rattrapage(page, search)
        return []

    if search.get("_rejet_appris"):
        await enrichir_rejet(page, search)
        return []

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
    print("Vinted V7.3 — PRECISION D’ABORD + vision + prix élargi + rejets appris")
    try:
        asyncio.run(vt.main())
    except KeyboardInterrupt:
        print("\nArret demande.")

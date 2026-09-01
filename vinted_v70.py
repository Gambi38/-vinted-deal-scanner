#!/usr/bin/env python3
# VERSION : VINTED_V711_FLASH_TRIAGE_OUVERT
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
import urllib.parse
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
        "max_items": 25,
        "rules": [rule],
        "_exemple_appris": True,
        "_profil_db": True,
    }


def ajouter_profils_db_aux_recherches(cfg):
    """
    V7.5 : évite de rescanner tous les profils appris à chaque run.
    On garde les profils les plus riches en priorité et on fait tourner le reste
    toutes les 5 minutes. Cela libère du temps pour les nouvelles annonces.
    """
    base = charger_base()
    profils = [
        p for p in base.get("profils", {}).values()
        if p.get("actif", True) and p.get("requete")
    ]
    if not profils:
        return 0

    profils.sort(
        key=lambda p: (
            int(p.get("nombre_exemples", 0)),
            int(bool(p.get("empreintes_visuelles"))),
            int(bool(p.get("prix_marche_bas"))),
        ),
        reverse=True,
    )

    # 5 profils par run : priorité aux annonces fraîches et aux vérifications profondes.
    limite = min(5, len(profils))
    tranche = int(datetime.now().timestamp() // 300)
    depart = (tranche * limite) % len(profils)
    selection = [profils[(depart + i) % len(profils)] for i in range(limite)]

    ajouts = [recherche_depuis_profil(p) for p in selection]
    if ajouts:
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
    """Détecte les cartouches multi-jeux sans confondre un dock USB 14-in-1."""
    t = n(f"{titre} {texte[:600]}")
    titre_n = n(titre)

    contexte_jeu = any(x in t for x in (
        "cartouche", "cartridge", "tarjeta", "scheda", "cartucho",
        "cartuccia", "r4", "nintendo ds", "3ds", "2ds", "game boy",
        "gameboy", "gba", "gbc", "jeux", "games", "juegos", "giochi",
    ))

    if contexte_jeu and re.search(r"\b\d{2,4}\s*(?:in|en)\s*1\b", titre_n):
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

    # Le prix de la carte suffit pour les annonces ordinaires. Le second
    # chargement de page n'est réservé qu'aux liens d'apprentissage utilisateur.
    if detail.get("ok") and not detail.get("price"):
        url_propre = str(url).split("?")[0]
        try:
            liens_apprentissage = {
                str(x).split("?")[0] for x in (_ancien_lire_exemples() + lire_rejets())
            }
        except Exception:
            liens_apprentissage = set()
        if url_propre in liens_apprentissage:
            prix = await _prix_vinted_fallback(page, url)
            if prix:
                detail["price"] = prix

    if detail.get("ok"):
        _enregistrer_observation_vendeur(detail, url)
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
    appliquer_niveau_prix_utilisateur(row)
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
        "triage_decision": row.get("triage_decision", ""),
        "triage_identite": row.get("triage_identite"),
        "triage_rentabilite": row.get("triage_rentabilite"),
        "triage_risque": row.get("triage_risque"),
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
    ("nintendo 3ds xl", ["nintendo 3ds xl", "nintendo 3 ds xl", "3ds xl", "3 ds xl"]),
    ("nintendo 3ds", ["nintendo 3ds", "nintendo 3 ds", "3ds", "3 ds"]),
    ("nintendo 2ds xl", ["nintendo 2ds xl", "nintendo 2 ds xl", "2ds xl", "2 ds xl"]),
    ("nintendo 2ds", ["nintendo 2ds", "nintendo 2 ds", "2ds", "2 ds"]),
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
    "switch": [
        "wii u", "wii", "gamecube", "n64", "3ds", "2ds", "nintendo ds",
        "game boy", "gba", "gbc", "ps5", "ps4", "ps3", "ps2", "playstation 3",
        "playstation 2", "xbox 360", "xbox one", "xbox series"
    ],
    "ps5": [
        "ps4", "ps3", "ps2", "playstation 4", "playstation 3", "playstation 2",
        "switch", "wii u", "wii", "3ds", "2ds", "nintendo ds", "xbox"
    ],
    "ps4": [
        "ps5", "ps3", "ps2", "playstation 5", "playstation 3", "playstation 2",
        "switch", "wii u", "wii", "3ds", "2ds", "nintendo ds", "xbox"
    ],
    "3ds": [
        "switch", "wii u", "wii", "nintendo ds", "ds lite", "dsi",
        "ps5", "ps4", "ps3", "ps2", "xbox", "game boy", "gba", "gbc"
    ],
    "ds": [
        "switch", "3ds", "2ds", "wii u", "wii", "ps5", "ps4", "ps3",
        "ps2", "xbox", "game boy", "gba", "gbc"
    ],
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


def accessoire_jeu_titre_v710(title):
    """Bloque les supports/stands de manette qui utilisent le nom d'un jeu."""
    t = n(title)
    controller_words = (
        "mando", "mand", "manette", "controller", "gamepad",
        "joycon", "joy-con", "dual sense", "dualsense",
    )
    support_words = (
        "soporte", "support", "stand", "holder", "porte manette",
        "porte-manette", "base para mando", "support de manette",
        "support manette", "controller stand", "gamepad stand",
    )
    return any(x in t for x in support_words) and any(x in t for x in controller_words)


def categorie_sanity_v69(category, title):
    # Jeux : blocage renforcé des objets dérivés.
    if str(category).startswith("JEU_"):
        if accessoire_jeu_titre_v710(title):
            return False, "support/accessoire de manette, pas le jeu"
        mauvais = hits(title, DERIVES_JEU)
        if mauvais:
            return False, "objet dérivé/accessoire: " + ", ".join(mauvais[:3])
        if category == "JEU_RETRO":
            ok_retro, raison_retro = retro_pokemon_titre_valide(title, "")
            if not ok_retro:
                return False, raison_retro
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
    if accessoire_jeu_titre_v710(title):
        return True, "accessoire_manette", ["support/stand de manette"], []
    vis = scores_vision_candidat(title)
    # Une photo très proche d'un rejet utilisateur bloque, sauf si elle est
    # au moins aussi proche d'un bon exemple. Le seuil élevé évite de confondre
    # deux cartouches/boîtes visuellement proches.
    if vis["negatif"] >= 0.93 and vis["negatif"] > vis["positif"] + 0.035:
        return True, "apprentissage_negatif_image", [f"photo rejet {vis['negatif']:.2f}"], []

    emb_seul, emb_raison = emballage_seul_multilingue(title, text)
    if emb_seul:
        return True, "emballage_seul_multilingue", [emb_raison], []

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
        # Une cartouche/disque sans boîte reste un vrai jeu.
        access_hits = [
            x for x in access_hits
            if n(x) not in {n(y) for y in JEU_SANS_BOITE_OK}
        ]
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
# V7.6 - FILTRE DE CONFIANCE / EMBALLAGES / VENDEURS
# ---------------------------------------------------------------------------
VENDEUR_OBSERVATIONS = {}

EMBALLAGE_SEUL_MOTS = (
    "doosje", "speldoosje", "game doosje", "lege doos", "doos zonder spel",
    "hoesje zonder spel", "empty case", "case only", "box only", "empty box",
    "boite vide", "boîte vide", "boitier vide", "boîtier vide",
    "caja vacia", "caja vacía", "solo caja", "scatola vuota", "solo scatola",
    "custodia vuota", "solo custodia", "leere hülle", "leere hulle",
    "nur hülle", "nur hulle", "nur verpackung", "lege verpakking",
)

EMBALLAGE_AVEC_JEU = (
    "avec jeu", "jeu inclus", "avec cartouche", "cartouche incluse", "complet",
    "complete", "completo", "met spel", "spel inbegrepen", "met cartridge",
    "with game", "game included", "with cartridge", "con juego", "juego incluido",
    "con cartucho", "con gioco", "gioco incluso", "mit spiel",
)

JEUX_SWITCH_PHARES = (
    "zelda", "mario kart 8", "mario odyssey", "super mario odyssey",
    "mario wonder", "super mario bros wonder", "luigi mansion 3", "luigi's mansion 3",
    "smash ultimate", "super smash bros ultimate", "pokemon arceus",
    "pokemon scarlet", "pokemon violet", "pokemon ecarlate", "pokemon violet",
    "metroid dread", "pikmin 4", "kirby forgotten", "fire emblem three houses",
    "xenoblade chronicles 2", "animal crossing new horizons",
)


def emballage_seul_multilingue(titre, texte=""):
    t = n(titre)
    full = n(f"{titre} {texte[:900]}")
    if any(x in full for x in EMBALLAGE_AVEC_JEU):
        return False, ""
    mot = next((x for x in EMBALLAGE_SEUL_MOTS if x in t), None)
    if mot:
        return True, mot
    # Le néerlandais "doos/doosje" est souvent utilisé pour vendre uniquement le boîtier.
    # Si le titre parle d'une boîte mais ne dit jamais qu'un jeu/cartouche est inclus, on rejette.
    if any(x in t for x in (" doos ", "doosje", "speldoos", "gamebox")):
        preuve_contenu = any(x in t for x in (
            "met spel", "met cartridge", "game included", "jeu inclus", "avec jeu",
            "cartouche incluse", "cartridge included",
        ))
        if not preuve_contenu:
            return True, "boîte/boîtier seul probable"
    return False, ""


def _enregistrer_observation_vendeur(detail, url):
    vendeur = n(detail.get("seller", ""))
    if not vendeur:
        return
    try:
        prix = round(float(detail.get("price") or 0), 2)
    except Exception:
        prix = 0.0
    VENDEUR_OBSERVATIONS.setdefault(vendeur, []).append({
        "url": str(url).split("?")[0],
        "titre": n(detail.get("title", "")),
        "prix": prix,
    })
    VENDEUR_OBSERVATIONS[vendeur] = VENDEUR_OBSERVATIONS[vendeur][-30:]


def risque_catalogue_vendeur(detail, categorie=""):
    vendeur = n(detail.get("seller", ""))
    obs = VENDEUR_OBSERVATIONS.get(vendeur, []) if vendeur else []
    if len(obs) < 3:
        return 0, ""
    prix = [x.get("prix") for x in obs if x.get("prix")]
    meme_prix = 0
    if prix:
        from collections import Counter as _C
        meme_prix = _C(prix).most_common(1)[0][1]
    retro = [x for x in obs if any(k in x.get("titre", "") for k in (
        "pokemon", "gba", "game boy", "gameboy", "gbc"
    ))]
    if categorie == "JEU_RETRO" and len(retro) >= 3 and meme_prix >= 3:
        return 32, "vendeur avec série de cartouches rétro au même prix"
    if len(obs) >= 5 and meme_prix >= 4:
        return 12, "vendeur avec nombreuses annonces au prix identique"
    return 0, ""


def switch_phare(titre, modele=""):
    t = n(f"{titre} {modele}")
    return any(n(x) in t for x in JEUX_SWITCH_PHARES)

# ---------------------------------------------------------------------------
# V7.5 - TRIAGE INTELLIGENT : POSITIF / A EVALUER / ELIMINER
# ---------------------------------------------------------------------------

def _mots_modele_utiles(texte):
    inutiles = {
        "vinted", "nintendo", "playstation", "sony", "microsoft", "jeu",
        "game", "console", "edition", "version", "switch", "ps5", "ps4",
        "xbox", "the", "of", "and", "pour", "avec", "sur",
    }
    return [
        x for x in re.findall(r"[a-z0-9]{2,}", n(texte))
        if x not in inutiles
    ][:8]


def evaluer_triage(row):
    """Retourne 3 scores 0-100 et une décision exploitable dans la notification."""
    url = str(row.get("url", "")).split("?")[0]
    detail = CACHE_DETAILS.get(url, {})
    titre = str(row.get("title", ""))
    texte = str(detail.get("text", ""))
    categorie = str(row.get("category", ""))
    modele = str(row.get("model", ""))

    try:
        prix = float(row.get("listing_price") or 0)
    except Exception:
        prix = 0.0
    try:
        revente = float(row.get("resale_low") or 0)
    except Exception:
        revente = 0.0
    try:
        marge = float(row.get("margin_low") or 0)
    except Exception:
        marge = 0.0
    try:
        roi = float(row.get("roi_low") or 0)
    except Exception:
        roi = 0.0

    # IDENTITE : une annonce arrivée ici a déjà passé les contrôles profonds.
    identite = 68.0
    mots_modele = _mots_modele_utiles(modele)
    titre_n = n(titre)
    if mots_modele:
        correspond = sum(1 for x in mots_modele if present(titre_n, x))
        ratio_modele = correspond / max(1, len(mots_modele))
        identite += ratio_modele * 18.0
    else:
        ratio_modele = 0.0

    # Description et preuves matérielles.
    full = n(f"{titre} {texte}")
    if categorie.startswith("JEU_") and any(x in full for x in (
        "jeu", "game", "cartouche", "cartridge", "disque", "disc",
        "switch", "ps5", "ps4", "3ds", "game boy", "gba",
    )):
        identite += 5.0
    if categorie == "CONSOLE" and titre_mene_par_produit(titre):
        identite += 7.0

    vis = scores_vision_candidat(titre)
    pos = float(vis.get("positif") or 0.0)
    neg = float(vis.get("negatif") or 0.0)
    if pos >= 0.95:
        identite += 12.0
    elif pos >= 0.90:
        identite += 8.0
    elif pos >= 0.84:
        identite += 4.0

    # RENTABILITE : le prix réel compte davantage que le score historique.
    if categorie == "JEU_SWITCH" and switch_phare(titre, modele):
        if prix <= 5:
            rentabilite = 100.0
        elif prix <= 10:
            rentabilite = 94.0
        elif prix <= 15:
            rentabilite = 82.0
        elif prix <= 18:
            rentabilite = 62.0
        else:
            rentabilite = 30.0
    elif categorie == "JEU_SWITCH" and revente > 0:
        ratio = prix / max(1.0, revente)
        rentabilite = 90.0 if ratio <= 0.35 else 78.0 if ratio <= 0.45 else 62.0 if ratio <= 0.55 else 40.0
    elif categorie == "ELECTRONIQUE" and any(x in full for x in (
        "ti 84", "ti-84", "ti nspire", "ti-nspire"
    )):
        rentabilite = 92.0 if prix <= 10 else 20.0
    elif categorie == "JEU_RETRO":
        if prix <= 10:
            rentabilite = 82.0
        elif prix <= 20:
            rentabilite = 62.0
        elif prix <= 25:
            rentabilite = 45.0
        else:
            rentabilite = 20.0
    elif revente > 0:
        ratio = prix / max(1.0, revente)
        if ratio <= 0.30:
            rentabilite = 96.0
        elif ratio <= 0.40:
            rentabilite = 88.0
        elif ratio <= 0.50:
            rentabilite = 78.0
        elif ratio <= 0.60:
            rentabilite = 66.0
        elif ratio <= 0.70:
            rentabilite = 52.0
        else:
            rentabilite = 35.0
        if marge >= 20:
            rentabilite += 5.0
        if roi >= 50:
            rentabilite += 4.0
    else:
        rentabilite = 45.0

    # RISQUE : 0 = très rassurant, 100 = à écarter.
    risque = 8.0
    risques_texte = n(str(row.get("risk", "")))
    if risques_texte:
        risque += 14.0

    vendeur_risque, vendeur_raison = risque_catalogue_vendeur(detail, categorie)
    if vendeur_risque:
        risque += vendeur_risque
        identite -= min(12.0, vendeur_risque * 0.35)

    # Une plateforme explicitement différente vaut beaucoup plus qu'une ressemblance de photo.
    if categorie.startswith("JEU_"):
        modele_n = n(modele)
        attendu = ""
        if "ps5" in modele_n or "playstation 5" in modele_n:
            attendu = "ps5"
        elif "ps4" in modele_n or "playstation 4" in modele_n:
            attendu = "ps4"
        elif "switch" in modele_n:
            attendu = "switch"
        elif "3ds" in modele_n:
            attendu = "3ds"
        elif "ds" in modele_n:
            attendu = "ds"
        if attendu:
            mauvais = hits(titre, PLATEFORMES_INCOMPATIBLES.get(attendu, []))
            bon = hits(titre, PLATEFORMES.get(attendu, []))
            if mauvais and not bon:
                risque += 60.0
                identite -= 55.0
    if not detail.get("image_url"):
        risque += 8.0
    if neg >= 0.90:
        risque += 22.0
    if neg >= 0.93 and neg > pos + 0.03:
        risque += 25.0
        identite -= 25.0
    if prix > 0 and revente > 0 and prix <= revente * 0.12:
        # Un prix absurde peut être un jackpot, mais aussi une arnaque/mauvais objet.
        risque += 18.0

    # Le rétro Pokémon est sensible aux repros : sans preuve forte, jamais auto-positif.
    pokemon_retro = categorie == "JEU_RETRO" and "pokemon" in full and any(
        x in full for x in ("gba", "game boy", "gameboy", "gbc")
    )
    preuve_originale = any(x in full for x in (
        "original", "originale", "authentique", "authentic", "genuine"
    ))
    if pokemon_retro:
        risque += 25.0
        if preuve_originale:
            risque -= 8.0
        identite -= 6.0

    identite = int(max(0, min(100, round(identite))))
    rentabilite = int(max(0, min(100, round(rentabilite))))
    risque = int(max(0, min(100, round(risque))))

    # Décision conservatrice : on préfère te demander plutôt que t'envoyer un faux jackpot.
    if pokemon_retro and not (preuve_originale and pos >= 0.94):
        decision = "A EVALUER"
    elif identite >= 82 and rentabilite >= 70 and risque <= 35:
        decision = "POSITIF"
    elif identite >= 58 and rentabilite >= 45 and risque <= 68:
        decision = "A EVALUER"
    else:
        decision = "ELIMINER"

    return {
        "decision": decision,
        "identite": identite,
        "rentabilite": rentabilite,
        "risque": risque,
        "vision_positive": round(pos, 3),
        "vision_negative": round(neg, 3),
        "vendeur_raison": vendeur_raison if 'vendeur_raison' in locals() else "",
    }


def preparer_triage_row(row):
    if row.get("_triage_prepare"):
        return row
    triage = evaluer_triage(row)
    row["_triage_prepare"] = True
    row["triage_decision"] = triage["decision"]
    row["triage_identite"] = triage["identite"]
    row["triage_rentabilite"] = triage["rentabilite"]
    row["triage_risque"] = triage["risque"]

    prefixe = (
        f"{triage['decision']} | identité {triage['identite']}% | "
        f"rentabilité {triage['rentabilite']}% | risque {triage['risque']}%"
    )
    ancienne_raison = str(row.get("reason", "")).strip()
    vendeur_raison = triage.get("vendeur_raison", "")
    extras = [x for x in (vendeur_raison, ancienne_raison) if x]
    row["reason"] = prefixe + (("; " + "; ".join(extras)) if extras else "")

    # Score visuel de la notification, distinct des 3 scores détaillés.
    if triage["decision"] == "POSITIF":
        row["opportunity_score"] = max(8, int(row.get("opportunity_score", 0)))
    elif triage["decision"] == "A EVALUER":
        row["opportunity_score"] = min(7, max(5, int(row.get("opportunity_score", 5))))
    else:
        row["opportunity_score"] = min(4, int(row.get("opportunity_score", 4)))
    return row


def append_alert_v75(row):
    preparer_triage_row(row)
    append_alert_v70(row)


def ntfy_send_v75(row):
    preparer_triage_row(row)
    decision = row.get("triage_decision", "A EVALUER")
    if decision == "ELIMINER":
        print(
            f"  X TRIAGE ELIMINER | {str(row.get('title',''))[:58]} | "
            f"identité={row.get('triage_identite')}% "
            f"rentabilité={row.get('triage_rentabilite')}% "
            f"risque={row.get('triage_risque')}%"
        )
        return False

    # Notification dédiée : le choix est lisible immédiatement.
    topic = str(__import__('os').getenv("NTFY_TOPIC", "")).strip()
    if not topic:
        return False
    server = str(__import__('os').getenv("NTFY_SERVER", "https://ntfy.sh")).rstrip("/")
    url = f"{server}/{urllib.parse.quote(topic, safe='')}"
    titre_ntfy = (
        f"{'✅ POSITIF' if decision == 'POSITIF' else '🟠 A EVALUER'} "
        f"{row.get('listing_price', 0):.2f} EUR"
    )
    body = (
        f"{row.get('title','')}\n"
        f"Identité {row.get('triage_identite')}% | "
        f"Rentabilité {row.get('triage_rentabilite')}% | "
        f"Risque {row.get('triage_risque')}%\n"
        f"Achat {row.get('listing_price',0):.2f} EUR | "
        f"Revente prudente {row.get('resale_low',0):.0f}-{row.get('resale_high',0):.0f} EUR | "
        f"Marge {row.get('margin_low',0):+.2f} EUR\n"
        f"{row.get('reason','')}\n{row.get('url','')}"
    )
    headers = {
        "Title": titre_ntfy,
        "Priority": "high" if decision == "POSITIF" else "default",
        "Tags": "white_check_mark,moneybag" if decision == "POSITIF" else "warning,mag",
        "Click": str(row.get("url", "")),
        "Actions": f"view, Ouvrir Vinted, {row.get('url','')}",
    }
    if row.get("image_url"):
        headers["Attach"] = str(row.get("image_url"))
    try:
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), method="POST", headers=headers
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"  ! ntfy triage: {e}")
        return False

# ---------------------------------------------------------------------------
# V7.4 - PRIX REALISTES / JEUX RETRO STRICTS
# ---------------------------------------------------------------------------

# Les cartouches seules sont de vrais jeux. On bloque la boîte VIDE, pas le jeu
# sans boîte. Ces termes ne doivent donc plus agir comme accessoires interdits.
JEU_SANS_BOITE_OK = {
    "cartouche seule", "cartouche nue", "loose cartridge", "cartridge only",
    "jeu sans boite", "jeu sans boîte", "sans boite", "sans boîte",
    "disque nu", "disc only", "game only", "jeu seul", "solo cartuccia",
    "solo scheda",
}

RETRO_POKEMON_SUSPECT = {
    "rom hack", "romhack", "hack", "fan game", "fangame", "fanmade",
    "custom", "repro", "reproduction", "clone", "bootleg", "copie",
    "neon", "chrome", "ambre", "gaulois", "distorsion",
    "cristal de jade", "duo emeraude", "new emeraude", "new rubis",
}

RETRO_POKEMON_OFFICIEL = (
    "pokemon emeraude", "pokemon version emeraude",
    "pokemon rubis", "pokemon version rubis",
    "pokemon saphir", "pokemon version saphir",
    "pokemon rouge feu", "pokemon version rouge feu",
    "pokemon vert feuille", "pokemon version vert feuille",
    "pokemon rouge", "pokemon version rouge",
    "pokemon bleu", "pokemon version bleue", "pokemon version bleu",
    "pokemon jaune", "pokemon version jaune",
    "pokemon or", "pokemon version or",
    "pokemon argent", "pokemon version argent",
    "pokemon cristal", "pokemon version cristal",
)

def retro_pokemon_titre_valide(titre, texte=""):
    t = n(f"{titre} {texte[:1200]}")
    if "pokemon" not in t and "pokémon" not in (titre or "").lower():
        return True, ""
    # On n'applique ce filtre qu'aux générations GB/GBC/GBA.
    contexte_retro = any(x in t for x in (
        "gba", "game boy advance", "gameboy advance", "gbc",
        "game boy color", "gameboy color", "game boy", "gameboy",
    ))
    if not contexte_retro:
        return True, ""
    if any(x in t for x in RETRO_POKEMON_SUSPECT):
        return False, "Pokemon rétro custom/repro/hack"
    if not any(x in t for x in RETRO_POKEMON_OFFICIEL):
        return False, "version Pokemon rétro non officielle/non reconnue"
    return True, ""

def appliquer_niveau_prix_utilisateur(row):
    """Transforme le prix en priorité lisible, sans gonfler la revente."""
    try:
        prix = float(row.get("listing_price"))
    except Exception:
        return row
    categorie = str(row.get("category", ""))
    titre = n(row.get("title", ""))
    niveau = ""
    score_min = None

    if categorie == "JEU_SWITCH" and switch_phare(row.get("title", ""), row.get("model", "")):
        if prix <= 5:
            niveau, score_min = "A NE PAS RATER", 10
        elif prix <= 10:
            niveau, score_min = "EXCELLENT PRIX", 9
        elif prix <= 15:
            niveau, score_min = "BON PRIX", 8
    elif categorie == "ELECTRONIQUE" and any(x in titre for x in (
        "ti 84", "ti-84", "ti nspire", "ti-nspire"
    )):
        if prix <= 10:
            niveau, score_min = "BON ACHAT CALCULATRICE", 9
    elif categorie == "JEU_RETRO":
        if prix <= 10:
            niveau, score_min = "RETRO TRES INTERESSANT", 8
        elif prix <= 20:
            niveau, score_min = "RETRO A VERIFIER", 7

    if niveau:
        try:
            row["opportunity_score"] = max(int(row.get("opportunity_score", 0)), score_min)
        except Exception:
            row["opportunity_score"] = score_min
        ancien = str(row.get("reason", "")).strip()
        row["reason"] = niveau + (("; " + ancien) if ancien else "")
    return row

# ---------------------------------------------------------------------------
# V7.3 - PRECISION D'ABORD
# ---------------------------------------------------------------------------
def appliquer_mode_precision(cfg):
    """
    V7.4 : précision d'abord, mais avec des plafonds cohérents par famille.
    Switch : on privilégie surtout <=15 EUR. Calculatrices TI : <=10 EUR.
    Rétro : prix et revente volontairement prudents à cause des repros.
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
        nom = n(search.get("name", ""))
        try:
            p = float(search.get("price_to")) if search.get("price_to") is not None else None
        except Exception:
            p = None

        # Seuils demandés : on veut d'abord les vrais bons achats, pas du volume.
        if categorie == "JEU_SWITCH":
            # Jusqu'à 18 EUR pour ne pas rater une vraie affaire légèrement >15,
            # mais les notifications 5/10/15 EUR sont fortement prioritaires.
            search["price_to"] = 18.0 if p is None else min(18.0, max(15.0, p))
        elif categorie == "JEU_RETRO":
            # Evite les cartouches custom/repro à ~35 EUR qui faussaient la marge.
            search["price_to"] = 25.0 if p is None else min(25.0, p)
        elif categorie == "ELECTRONIQUE" and any(x in nom for x in (
            "ti 84", "ti-84", "ti nspire", "ti-nspire"
        )):
            search["price_to"] = 10.0
        elif p and p > 0:
            if categorie.startswith("JEU_"):
                search["price_to"] = round(min(p * 1.10, p + 8.0), 2)
            elif categorie == "CONSOLE":
                search["price_to"] = round(min(p * 1.12, p + 20.0), 2)
            elif categorie == "ELECTRONIQUE":
                search["price_to"] = round(min(p * 1.10, p + 20.0), 2)

        for rule in search.get("rules", []):
            if not isinstance(rule, dict):
                continue

            if categorie == "JEU_RETRO":
                # L'ancien 80-180 EUR était trop optimiste et mélangeait complet,
                # cartouche seule, repros et hacks. Valeur utilisée seulement pour
                # un calcul prudent tant que l'authenticité n'est pas prouvée.
                bas = rule.get("resale_low")
                haut = rule.get("resale_high")
                try:
                    rule["resale_low"] = min(float(bas), 35.0) if bas is not None else 35.0
                except Exception:
                    rule["resale_low"] = 35.0
                try:
                    rule["resale_high"] = min(float(haut), 50.0) if haut is not None else 50.0
                except Exception:
                    rule["resale_high"] = 50.0
                rule["max_buy_ratio"] = min(float(rule.get("max_buy_ratio", 0.45)), 0.55)
                rule["min_margin"] = max(8.0, min(float(rule.get("min_margin", 10)), 12.0))
                rule["min_roi_pct"] = max(30.0, float(rule.get("min_roi_pct", 30)))
                rule["authenticity_risk"] = True
                # Cartouche seule autorisée; seules les boîtes vides restent rejetées.
                rule["exclude"] = [
                    x for x in rule.get("exclude", [])
                    if n(x) not in {n(y) for y in JEU_SANS_BOITE_OK}
                ]
                continue

            try:
                actuel_ratio = float(rule.get("max_buy_ratio", 0.40))
            except Exception:
                actuel_ratio = 0.40

            if categorie == "JEU_SWITCH":
                rule["max_buy_ratio"] = max(actuel_ratio, 0.55)
                rule["min_margin"] = min(float(rule.get("min_margin", 10)), 8.0)
                rule["min_roi_pct"] = min(float(rule.get("min_roi_pct", 30)), 20.0)
            elif categorie.startswith("JEU_"):
                rule["max_buy_ratio"] = max(actuel_ratio, 0.55)
                rule["min_margin"] = min(float(rule.get("min_margin", 10)), 8.0)
                rule["min_roi_pct"] = min(float(rule.get("min_roi_pct", 30)), 22.0)
            elif categorie == "CONSOLE":
                rule["max_buy_ratio"] = max(actuel_ratio, 0.55)
                rule["min_margin"] = min(float(rule.get("min_margin", 20)), 15.0)
                rule["min_roi_pct"] = min(float(rule.get("min_roi_pct", 30)), 22.0)
            elif categorie == "ELECTRONIQUE":
                rule["max_buy_ratio"] = max(actuel_ratio, 0.50)
                rule["min_margin"] = min(float(rule.get("min_margin", 20)), 12.0)
                rule["min_roi_pct"] = min(float(rule.get("min_roi_pct", 30)), 22.0)


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
    Une seule patrouille par run pour garder la majorité du budget aux recherches ciblées.
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
    selection = [cles[depart]]

    recherches = []
    for cle in selection:
        g = groupes[cle]
        recherches.append({
            "name": f"VISION - chasse {cle}",
            "category": "VISION",
            "query": g["query"],
            "price_to": g["price_to"],
            "max_items": 10,
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
        if ouvertes >= 3:
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


# ---------------------------------------------------------------------------
# V7.6 - ne mélange plus ELIMINER avec les vraies alertes
# ---------------------------------------------------------------------------
def append_alert_v76(row):
    preparer_triage_row(row)
    decision = row.get("triage_decision", "A EVALUER")
    if decision == "ELIMINER":
        url = str(row.get("url", "")).split("?")[0]
        detail = CACHE_DETAILS.get(url, {})
        ajouter_historique({
            "origine": "triage_elimine",
            "confiance": 0.10,
            "positif_utilisateur": False,
            "item_id": row.get("item_id", ""),
            "url": url,
            "title": row.get("title", ""),
            "price": row.get("listing_price"),
            "category": row.get("category", ""),
            "model": row.get("model", ""),
            "triage_decision": decision,
            "triage_identite": row.get("triage_identite"),
            "triage_rentabilite": row.get("triage_rentabilite"),
            "triage_risque": row.get("triage_risque"),
            "description": (detail.get("text") or "")[:1200],
            "image_url": detail.get("image_url", ""),
        })
        return
    append_alert_v70(row)


async def scan_search_v76(page, search, cfg, blacklist, seen_ids):
    rows = await scan_search_v69(page, search, cfg, blacklist, seen_ids)
    propres = []
    for row in rows or []:
        preparer_triage_row(row)
        if row.get("triage_decision") != "ELIMINER":
            propres.append(row)
    return propres


# ---------------------------------------------------------------------------
# V7.7 - FASTPATH : moins de détours, marché prudent, vision à la demande
# ---------------------------------------------------------------------------
# Objectifs :
# - recherches essentielles d'abord ;
# - variantes/typos tournantes au lieu de toutes les rescanner à chaque run ;
# - 3 profils appris max par run ;
# - patrouille visuelle seulement 1 run sur 4, en fin de parcours ;
# - pas de téléchargement du modèle ONNX pour chaque annonce :
#   hash d'image rapide d'abord, réseau seulement sur une vraie hésitation ;
# - prix de revente rabotés avant triage pour ne plus afficher de marge optimiste.

VISION_PROFONDE_UTILISEE = 0
BASE_TRIAGE_CACHE = None


def _empreinte_visuelle_rapide(image_url):
    """Empreinte peu coûteuse : téléchargement image + deux hashes, sans ONNX."""
    if not image_url:
        return None
    im = _telecharger_image(image_url)
    if im is None:
        return None
    ah, dh = _hash_visuel(im)
    if not ah and not dh:
        return None
    return {
        "image_url": image_url,
        "embedding": "",
        "ahash": ah,
        "dhash": dh,
    }


async def verify_listing_v77(page, url, fallback_title=""):
    """Même vérification profonde, mais la vision lourde n'est plus systématique."""
    detail = await _ancien_verify_listing(page, url, fallback_title)

    if detail.get("ok") and not detail.get("price"):
        prix = await _prix_vinted_fallback(page, url)
        if prix:
            detail["price"] = prix

    if detail.get("ok"):
        _enregistrer_observation_vendeur(detail, url)
        image_url = detail.get("image_url") or ""
        emp = _empreinte_visuelle_rapide(image_url) if image_url else None
        cle_titre = n(detail.get("title") or fallback_title)
        if emp:
            VISION_PAR_TITRE[cle_titre] = emp
            detail["vision_disponible"] = True
            detail["vision_mode"] = "rapide"
        else:
            detail["vision_disponible"] = False
            detail["vision_mode"] = "aucune"
        CACHE_DETAILS[str(url).split("?")[0]] = detail
    return detail


def _forcer_vision_complete_pour_url(url, negatif=False):
    """Utilisé uniquement lors d'un nouvel apprentissage utilisateur."""
    url = str(url or "").split("?")[0]
    detail = CACHE_DETAILS.get(url, {})
    image_url = detail.get("image_url") or ""
    titre = detail.get("title") or ""
    if not image_url:
        return False

    emp = _empreinte_visuelle(image_url)
    if not emp:
        return False
    VISION_PAR_TITRE[n(titre)] = emp

    base = charger_base()
    cle = "profils_negatifs" if negatif else "profils"
    champ_liens = "liens" if negatif else "liens_exemples"
    modifie = False
    for pid, profil in base.get(cle, {}).items():
        if url in [str(x).split("?")[0] for x in profil.get(champ_liens, [])]:
            ajouter_empreinte_profil(profil, emp, image_url)
            profil["mis_a_jour"] = datetime.now().isoformat(timespec="seconds")
            base[cle][pid] = profil
            modifie = True
    if modifie:
        sauver_base(base)
    return modifie


_enrichir_exemple_v76 = enrichir_exemple
_enrichir_rejet_v76 = enrichir_rejet

async def enrichir_exemple(page, search):
    await _enrichir_exemple_v76(page, search)
    rules = search.get("rules", [])
    if rules:
        url = rules[0].get("source_exemple", "")
        if url:
            _forcer_vision_complete_pour_url(url, negatif=False)


async def enrichir_rejet(page, search):
    url = str(search.get("_source_rejet", "")).split("?")[0]
    await _enrichir_rejet_v76(page, search)
    if url:
        _forcer_vision_complete_pour_url(url, negatif=True)


def _nettoyer_requete_v77(q):
    q = re.sub(r"\|\s*vinted\b", " ", str(q or ""), flags=re.I)
    q = re.sub(r"\bvinted\b", " ", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip(" -|/")
    return q


def recherche_depuis_profil_v77(profil, rafraichir_marche=False):
    search = recherche_depuis_profil(profil)
    modele_propre = _nettoyer_requete_v77(profil.get("modele", ""))
    requete = _nettoyer_requete_v77(profil.get("requete") or modele_propre)
    if requete:
        search["query"] = requete
    search["name"] = f"BASE - {modele_propre or requete}"
    search["max_items"] = 15

    rule = search.get("rules", [{}])[0]
    if modele_propre:
        rule["model"] = modele_propre
        rule["label"] = f"Profil appris : {modele_propre}"

    bas = profil.get("prix_marche_bas")
    haut = profil.get("prix_marche_haut")
    categorie = str(profil.get("categorie", ""))

    if bas and not rafraichir_marche:
        # Réutilise le marché persistant : pas besoin de recalibrer à chaque run.
        rule["auto_market"] = False
        rule["resale_low"] = float(bas)
        rule["resale_high"] = float(haut or bas)
        try:
            basf = float(bas)
            if categorie == "JEU_SWITCH":
                search["price_to"] = min(18.0, basf * 0.55)
            elif categorie == "JEU_RETRO":
                search["price_to"] = min(20.0, basf * 0.45)
            elif categorie.startswith("JEU_"):
                search["price_to"] = min(30.0, basf * 0.55)
            elif categorie == "CONSOLE":
                search["price_to"] = basf * 0.55
            elif categorie == "ELECTRONIQUE":
                search["price_to"] = basf * 0.50
        except Exception:
            pass
    else:
        rule["auto_market"] = True
        # Sans marché connu, on évite quand même une recherche illimitée si
        # l'utilisateur a déjà donné un prix positif de référence.
        cible = profil.get("prix_cible_max")
        if cible:
            try:
                search["price_to"] = round(float(cible) * 1.15, 2)
            except Exception:
                pass

    return search


def _selection_profils_v77(statiques=None):
    """
    Choisit jusqu'à 3 profils appris qui ne doublonnent pas une recherche
    statique déjà prévue dans ce cycle.
    """
    from difflib import SequenceMatcher

    base = charger_base()
    profils = [
        p for p in base.get("profils", {}).values()
        if p.get("actif", True) and (p.get("requete") or p.get("modele"))
    ]
    if not profils:
        return []

    profils.sort(
        key=lambda p: (
            int(p.get("nombre_exemples", 0)),
            int(bool(p.get("empreintes_visuelles"))),
            int(bool(p.get("prix_marche_bas"))),
        ),
        reverse=True,
    )

    requetes_statiques = [
        n(_nettoyer_requete_v77(x.get("query", "")))
        for x in (statiques or [])
        if x.get("query")
    ]

    def deja_couvert(q):
        nq = n(_nettoyer_requete_v77(q))
        if not nq:
            return True
        for sq in requetes_statiques:
            if nq == sq:
                return True
            if SequenceMatcher(None, nq, sq).ratio() >= 0.84:
                return True
        return False

    tranche = int(datetime.now().timestamp() // 300)
    depart = (tranche * 3) % len(profils)
    selection = []
    for offset in range(len(profils)):
        p = profils[(depart + offset) % len(profils)]
        q = p.get("requete") or p.get("modele", "")
        if deja_couvert(q):
            continue
        selection.append(p)
        if len(selection) >= 3:
            break
    return selection

def _rotation_aliases_statiques_v77(searches):
    """
    Les filtres du type "FILTRE - X / variante" ne passent plus tous dans le
    même cycle. Une variante tourne toutes les 5 minutes.
    """
    tranche = int(datetime.now().timestamp() // 300)
    groupes = {}
    autres = []
    vus = set()

    for s in searches:
        if not isinstance(s, dict):
            continue
        q = _nettoyer_requete_v77(s.get("query", ""))
        cat = str(s.get("category", ""))
        cle_exacte = (cat, n(q))
        if q and cle_exacte in vus:
            continue
        if q:
            vus.add(cle_exacte)

        nom = str(s.get("name", ""))
        if nom.startswith("FILTRE - ") and " / " in nom:
            cle = nom.rsplit(" / ", 1)[0]
            groupes.setdefault(cle, []).append(s)
        else:
            autres.append(s)

    selection = list(autres)
    for cle, variantes in groupes.items():
        variantes = sorted(variantes, key=lambda x: n(x.get("query", "")))
        idx = (tranche + int(hashlib.sha1(cle.encode("utf-8")).hexdigest()[:6], 16)) % len(variantes)
        selection.append(variantes[idx])

    def priorite(s):
        nom = str(s.get("name", ""))
        cat = str(s.get("category", ""))
        if nom.startswith("FILTRE - "):
            return 0
        if cat == "JEU_SWITCH":
            return 1
        if cat == "JEU_PS5":
            return 2
        if cat == "CONSOLE":
            return 3
        if cat.startswith("JEU_"):
            return 4
        if cat == "ELECTRONIQUE":
            return 5
        return 6

    selection.sort(key=lambda s: (priorite(s), n(s.get("name", ""))))

    for s in selection:
        # Les cartes utiles sont presque toujours dans les plus récentes.
        try:
            m = int(s.get("max_items", 15))
            s["max_items"] = min(m, 24)
        except Exception:
            s["max_items"] = 15
    return selection


def appliquer_exemples_v77(cfg):
    # 1) Corrige d'abord les plafonds/prix des recherches natives.
    appliquer_mode_precision(cfg)
    statiques = _rotation_aliases_statiques_v77(list(cfg.get("searches", [])))

    # 2) Un seul nouvel exemple positif par run.
    classes = liens_deja_classes()
    exemples = []
    for url in _ancien_lire_exemples():
        if url in classes:
            continue
        r = vt.convertir_exemple_en_recherche(url)
        if r:
            exemples.append(r)
        if len(exemples) >= 1:
            break

    # 3) Un seul rejet utilisateur à apprendre par run.
    tmp_rejets = {"searches": []}
    classes_rej = liens_rejetes_deja_classes()
    pendant_rejet = next((u for u in lire_rejets() if u not in classes_rej), None)
    if pendant_rejet:
        tmp_rejets["searches"].append({
            "name": f"REJET - {vt.titre_depuis_lien_exemple(pendant_rejet)}",
            "category": "REJET",
            "query": "",
            "price_to": None,
            "max_items": 0,
            "rules": [],
            "_rejet_appris": True,
            "_source_rejet": pendant_rejet,
            "_priorite_personnelle": True,
        })

    # 4) Un seul rattrapage ancien au lieu de trois.
    tmp_rattr = {"searches": []}
    base = charger_base()
    for pid, profil in base.get("profils", {}).items():
        manque_prix = not profil.get("prix_cible_max")
        manque_vision = not profil.get("empreintes_visuelles")
        liens = profil.get("liens_exemples", [])
        if (manque_prix or manque_vision) and liens:
            tmp_rattr["searches"].append({
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
            break

    # 5) Trois profils appris, marché recalculé très rarement.
    profils = _selection_profils_v77(statiques)
    tranche = int(datetime.now().timestamp() // 300)
    appris = []
    for i, profil in enumerate(profils):
        refresh = bool(tranche % 12 == 0 and i == 0)  # ~1 fois/heure max.
        appris.append(recherche_depuis_profil_v77(profil, rafraichir_marche=refresh))

    # 6) Vision large seulement 1 cycle sur 4 et EN FIN de parcours.
    vision = []
    if tranche % 4 == 0:
        tmp_vis = {"searches": []}
        if ajouter_patrouilles_vision(tmp_vis):
            vision = tmp_vis["searches"][:1]
            for s in vision:
                s["max_items"] = min(int(s.get("max_items", 6)), 6)

    # Ordre FASTPATH : vraies recherches d'abord. Les tâches de maintenance
    # ne doivent plus retarder les bonnes affaires fraîches.
    cfg["searches"] = (
        statiques
        + exemples
        + tmp_rejets["searches"]
        + tmp_rattr["searches"]
        + appris
        + vision
    )

    # Temps d'attente légèrement réduit, sans accélération agressive anti-bot.
    try:
        cfg["page_wait_ms"] = min(int(cfg.get("page_wait_ms", 900)), 700)
    except Exception:
        cfg["page_wait_ms"] = 700

    if appris:
        print(f"[INFO] FASTPATH: {len(appris)} profil(s) appris en rotation.")
    if vision:
        print("[INFO] FASTPATH: 1 patrouille visuelle légère en fin de cycle.")
    if tmp_rejets["searches"]:
        print("[INFO] FASTPATH: 1 faux positif à mémoriser.")
    if tmp_rattr["searches"]:
        print("[INFO] FASTPATH: 1 ancien profil à compléter.")
    return len(exemples)


_calibrer_v76 = calibrer_regles_exemple_v69

def calibrer_regles_exemple_v77(search, cards, blacklist):
    """Marché appris moins optimiste + confiance explicite."""
    _calibrer_v76(search, cards, blacklist)

    categorie = str(search.get("category", ""))
    for rule in search.get("rules", []):
        if not rule.get("auto_market") or rule.get("resale_low") is None:
            continue

        # Recompte les comparables plausibles sans nouvelle requête réseau.
        valides = 0
        for c in cards:
            try:
                titre = c.get("title", "")
                contenu = c.get("text", "")
                p = vt.parse_price(contenu)
                if p is None or p <= 2 or p > 350:
                    continue
                if not rule_match_v69(rule, titre, contenu, deep=False):
                    continue
                sane, _ = categorie_sanity_v69(categorie, titre)
                if not sane:
                    continue
                emb, _ = emballage_seul_multilingue(titre, contenu)
                if emb:
                    continue
                valides += 1
            except Exception:
                continue

        if valides >= 15:
            confiance = 85
        elif valides >= 8:
            confiance = 72
        elif valides >= 4:
            confiance = 58
        else:
            confiance = 40

        # Les comparables Vinted sont des prix affichés, pas des ventes réalisées.
        # On applique donc un rabais prudent avant de s'en servir pour la marge.
        facteur_bas = 0.90 if valides >= 8 else 0.85
        facteur_haut = 0.93 if valides >= 8 else 0.90
        bas = round(float(rule["resale_low"]) * facteur_bas, 2)
        haut = round(max(bas, float(rule.get("resale_high") or rule["resale_low"]) * facteur_haut), 2)

        if categorie == "JEU_RETRO":
            bas = min(bas, 28.0)
            haut = min(max(bas, haut), 38.0)

        rule["resale_low"] = bas
        rule["resale_high"] = haut
        rule["_confiance_marche"] = confiance
        rule["_nb_comparables_marche"] = valides

        pid = rule.get("_profil_id")
        if pid:
            base = charger_base()
            profil = base.get("profils", {}).get(pid)
            if profil:
                profil["prix_marche_bas"] = bas
                profil["prix_marche_haut"] = haut
                profil["confiance_marche"] = confiance
                profil["nb_comparables_marche"] = valides
                profil["marche_mis_a_jour"] = datetime.now().isoformat(timespec="seconds")
                base["profils"][pid] = profil
                sauver_base(base)

        print(
            f"  + MARCHE PRUDENT V7.7 | {rule.get('model','')[:45]} | "
            f"{valides} comparables | confiance {confiance}% | {bas:.2f}-{haut:.2f} EUR"
        )


def _base_triage_v77():
    global BASE_TRIAGE_CACHE
    if BASE_TRIAGE_CACHE is None:
        BASE_TRIAGE_CACHE = charger_base()
    return BASE_TRIAGE_CACHE


def _profil_pour_row_v77(row):
    modele = n(_nettoyer_requete_v77(row.get("model", "")))
    recherche = n(str(row.get("search", "")))
    if not modele and "base -" not in recherche:
        return None
    meilleur = None
    meilleur_score = 0.0
    for p in _base_triage_v77().get("profils", {}).values():
        pm = n(_nettoyer_requete_v77(p.get("modele", "")))
        if not pm:
            continue
        if modele and (modele == pm or modele in pm or pm in modele):
            return p
        # secours léger par tokens
        a = set(_mots_modele_utiles(modele))
        b = set(_mots_modele_utiles(pm))
        if a and b:
            sc = len(a & b) / max(1, len(a | b))
            if sc > meilleur_score:
                meilleur_score, meilleur = sc, p
    return meilleur if meilleur_score >= 0.55 else None


def _confiance_marche_v77(row):
    categorie = str(row.get("category", ""))
    if categorie == "JEU_SWITCH" and switch_phare(row.get("title", ""), row.get("model", "")):
        return 88

    p = _profil_pour_row_v77(row)
    if p:
        try:
            return int(p.get("confiance_marche") or (65 if p.get("prix_marche_bas") else 45))
        except Exception:
            return 45

    # Règles statiques configurées manuellement : utiles mais pas prix vendus.
    if categorie == "JEU_RETRO":
        return 45
    if categorie:
        return 72
    return 45


def appliquer_revente_prudente_v77(row):
    if row.get("_revente_v77"):
        return row
    row["_revente_v77"] = True

    try:
        low = float(row.get("resale_low") or 0)
        high = float(row.get("resale_high") or low)
        total = float(row.get("total_buy_est") or 0)
    except Exception:
        return row
    if low <= 0:
        return row

    categorie = str(row.get("category", ""))
    learned = str(row.get("search", "")).startswith("BASE -")

    if categorie == "JEU_RETRO":
        facteur = 0.75
        low = min(low, 35.0)
        high = min(high, 45.0)
    elif learned:
        facteur = 0.88
    elif categorie.startswith("JEU_"):
        facteur = 0.92
    elif categorie in {"CONSOLE", "ELECTRONIQUE"}:
        facteur = 0.90
    else:
        facteur = 0.90

    plow = round(low * facteur, 2)
    phigh = round(max(plow, high * facteur), 2)
    marge_low = round(plow - total, 2)
    marge_high = round(phigh - total, 2)
    roi = round((marge_low / total * 100), 1) if total > 0 else 0.0

    row["resale_low"] = plow
    row["resale_high"] = phigh
    row["margin_low"] = marge_low
    row["margin_high"] = marge_high
    row["roi_low"] = roi
    return row


_evaluer_triage_v76 = evaluer_triage

def evaluer_triage_v77(row):
    appliquer_revente_prudente_v77(row)
    tri = _evaluer_triage_v76(row)
    categorie = str(row.get("category", ""))
    prix = float(row.get("listing_price") or 0)
    confiance_marche = _confiance_marche_v77(row)
    tri["confiance_marche"] = confiance_marche

    titre = row.get("title", "")
    modele = row.get("model", "")
    phare_switch = categorie == "JEU_SWITCH" and switch_phare(titre, modele)

    # Une boîte vide multilingue ne passe jamais en "à évaluer".
    detail = CACHE_DETAILS.get(str(row.get("url", "")).split("?")[0], {})
    emballage, _ = emballage_seul_multilingue(titre, detail.get("text", ""))
    if emballage:
        tri["decision"] = "ELIMINER"
        tri["risque"] = max(tri["risque"], 95)
        tri["identite"] = min(tri["identite"], 15)
        return tri

    # Règles utilisateur très lisibles pour les jeux Switch phares.
    if phare_switch:
        if prix <= 15 and tri["identite"] >= 80 and tri["risque"] <= 30:
            tri["decision"] = "POSITIF"
        elif prix <= 18 and tri["identite"] >= 62 and tri["risque"] <= 62:
            tri["decision"] = "A EVALUER"
        else:
            tri["decision"] = "ELIMINER"

    # Calculatrices : l'utilisateur veut essentiellement les achats <= 10 EUR.
    elif categorie == "ELECTRONIQUE" and any(x in n(f"{titre} {modele}") for x in (
        "ti 84", "ti-84", "ti nspire", "ti-nspire"
    )):
        if prix <= 10 and tri["identite"] >= 82 and tri["risque"] <= 35:
            tri["decision"] = "POSITIF"
        elif prix <= 10 and tri["identite"] >= 58 and tri["risque"] <= 65:
            tri["decision"] = "A EVALUER"
        else:
            tri["decision"] = "ELIMINER"

    # Rétro : jamais d'auto-achat sur une simple estimation Vinted.
    elif categorie == "JEU_RETRO":
        if (
            prix <= 20
            and tri["identite"] >= 62
            and tri["rentabilite"] >= 55
            and tri["risque"] <= 65
        ):
            tri["decision"] = "A EVALUER"
        else:
            tri["decision"] = "ELIMINER"

    else:
        # Un marché faible ne suffit jamais pour un POSITIF automatique.
        if tri["decision"] == "POSITIF" and confiance_marche < 60:
            tri["decision"] = "A EVALUER"

        if categorie == "CONSOLE" and tri["decision"] == "POSITIF":
            if not (
                tri["identite"] >= 88
                and tri["rentabilite"] >= 65
                and tri["risque"] <= 28
                and confiance_marche >= 60
            ):
                tri["decision"] = "A EVALUER"

        # "A EVALUER" doit rester une shortlist attractive, pas une poubelle.
        if tri["decision"] == "A EVALUER":
            if (
                tri["identite"] < 60
                or tri["rentabilite"] < 52
                or tri["risque"] > 68
            ):
                tri["decision"] = "ELIMINER"

    tri["confiance_globale"] = int(round(
        tri["identite"] * 0.45
        + tri["rentabilite"] * 0.35
        + (100 - tri["risque"]) * 0.20
    ))
    return tri


def _vision_profonde_si_utile_v77(row, tri):
    """Une seule analyse ONNX par run, uniquement pour une vraie hésitation attractive."""
    global VISION_PROFONDE_UTILISEE
    if VISION_PROFONDE_UTILISEE >= 1:
        return False
    if tri.get("decision") != "A EVALUER":
        return False
    if tri.get("rentabilite", 0) < 72:
        return False
    if not (50 <= tri.get("identite", 0) <= 86):
        return False

    url = str(row.get("url", "")).split("?")[0]
    detail = CACHE_DETAILS.get(url, {})
    image_url = detail.get("image_url") or row.get("image_url", "")
    titre = detail.get("title") or row.get("title", "")
    if not image_url:
        return False

    VISION_PROFONDE_UTILISEE += 1
    emp = _empreinte_visuelle(image_url)
    if not emp or not emp.get("embedding"):
        return False
    VISION_PAR_TITRE[n(titre)] = emp
    detail["vision_mode"] = "profonde"
    CACHE_DETAILS[url] = detail
    return True


def preparer_triage_row_v77(row):
    if row.get("_triage_v77"):
        return row

    # Enlève un éventuel triage V7.6 calculé avant la correction prudente.
    row.pop("_triage_prepare", None)
    appliquer_revente_prudente_v77(row)
    triage = evaluer_triage_v77(row)

    # Si l'annonce est vraiment attractive mais ambiguë, une seule analyse
    # visuelle profonde peut la faire monter/descendre. Pas de détour systématique.
    if _vision_profonde_si_utile_v77(row, triage):
        triage = evaluer_triage_v77(row)

    row["_triage_prepare"] = True
    row["_triage_v77"] = True
    row["triage_decision"] = triage["decision"]
    row["triage_identite"] = triage["identite"]
    row["triage_rentabilite"] = triage["rentabilite"]
    row["triage_risque"] = triage["risque"]
    row["triage_marche"] = triage.get("confiance_marche", 45)
    row["triage_confiance"] = triage.get("confiance_globale", 0)

    prefixe = (
        f"{triage['decision']} | identité {triage['identite']}% | "
        f"rentabilité {triage['rentabilite']}% | risque {triage['risque']}% | "
        f"marché {row['triage_marche']}%"
    )
    ancienne = str(row.get("reason", "")).strip()
    # Évite d'empiler les anciens préfixes de triage si la ligne a été recalculée.
    ancienne = re.sub(
        r"^(?:POSITIF|A EVALUER|ELIMINER)\s*\|.*?(?=(?:A NE PAS RATER|EXCELLENT PRIX|BON PRIX|RETRO|prix a|vendeur|$))",
        "",
        ancienne,
        flags=re.I,
    ).strip(" ;")
    row["reason"] = prefixe + (("; " + ancienne) if ancienne else "")

    if triage["decision"] == "POSITIF":
        row["opportunity_score"] = max(8, int(row.get("opportunity_score", 0)))
    elif triage["decision"] == "A EVALUER":
        row["opportunity_score"] = min(7, max(5, int(row.get("opportunity_score", 5))))
    else:
        row["opportunity_score"] = min(4, int(row.get("opportunity_score", 4)))
    return row


def append_alert_v77(row):
    preparer_triage_row_v77(row)
    if row.get("triage_decision") == "ELIMINER":
        url = str(row.get("url", "")).split("?")[0]
        detail = CACHE_DETAILS.get(url, {})
        ajouter_historique({
            "origine": "triage_elimine_v77",
            "confiance": 0.10,
            "positif_utilisateur": False,
            "item_id": row.get("item_id", ""),
            "url": url,
            "title": row.get("title", ""),
            "price": row.get("listing_price"),
            "category": row.get("category", ""),
            "model": row.get("model", ""),
            "triage_decision": "ELIMINER",
            "triage_identite": row.get("triage_identite"),
            "triage_rentabilite": row.get("triage_rentabilite"),
            "triage_risque": row.get("triage_risque"),
            "triage_marche": row.get("triage_marche"),
            "description": (detail.get("text") or "")[:1000],
            "image_url": detail.get("image_url", ""),
        })
        return
    append_alert_v70(row)


def ntfy_send_v77(row):
    preparer_triage_row_v77(row)
    decision = row.get("triage_decision", "A EVALUER")
    if decision == "ELIMINER":
        print(
            f"  X TRIAGE V7.7 | {str(row.get('title',''))[:56]} | "
            f"id={row.get('triage_identite')} rent={row.get('triage_rentabilite')} "
            f"risque={row.get('triage_risque')} marché={row.get('triage_marche')}"
        )
        return False

    topic = str(__import__('os').getenv("NTFY_TOPIC", "")).strip()
    if not topic:
        return False
    server = str(__import__('os').getenv("NTFY_SERVER", "https://ntfy.sh")).rstrip("/")
    url = f"{server}/{urllib.parse.quote(topic, safe='')}"

    prix = float(row.get("listing_price", 0) or 0)
    niveau = ""
    if row.get("category") == "JEU_SWITCH" and switch_phare(row.get("title",""), row.get("model","")):
        if prix <= 5:
            niveau = "A NE PAS RATER"
        elif prix <= 10:
            niveau = "EXCELLENT"
        elif prix <= 15:
            niveau = "BON PRIX"

    titre_ntfy = (
        ("POSITIF" if decision == "POSITIF" else "A EVALUER")
        + (f" | {niveau}" if niveau else "")
        + f" | {prix:.2f} EUR"
    ).encode("ascii", "ignore").decode("ascii")
    body = (
        f"{row.get('title','')}\n"
        f"Identité {row.get('triage_identite')}% | "
        f"Rentabilité {row.get('triage_rentabilite')}% | "
        f"Risque {row.get('triage_risque')}% | "
        f"Marché {row.get('triage_marche')}%\n"
        f"Achat livré estimé {row.get('total_buy_est',0):.2f} EUR | "
        f"Revente prudente {row.get('resale_low',0):.0f}-{row.get('resale_high',0):.0f} EUR | "
        f"Marge prudente {row.get('margin_low',0):+.2f} EUR\n"
        f"{row.get('reason','')}\n{row.get('url','')}"
    )
    headers = {
        "Title": titre_ntfy,
        "Priority": "high" if decision == "POSITIF" else "default",
        "Tags": "white_check_mark,moneybag" if decision == "POSITIF" else "warning,mag",
        "Click": str(row.get("url", "")),
        "Actions": f"view, Ouvrir Vinted, {row.get('url','')}",
    }
    if row.get("image_url"):
        headers["Attach"] = str(row.get("image_url"))
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"  ! ntfy V7.7: {e}")
        return False


async def scan_patrouille_vision_v77(page, search, cfg, blacklist, seen_ids):
    """
    Garde la capacité de retrouver une annonce mal nommée, mais sans chemin long :
    6 cartes max, 1 seule page détaillée.
    """
    # Réutilise la fonction V7.6 en abaissant temporairement les limites.
    search = dict(search)
    search["max_items"] = min(6, int(search.get("max_items", 6)))

    # La fonction V7.6 a un plafond interne de 3 ouvertures. Pour éviter ce détour,
    # on fait une version compacte du préfiltre puis une seule vérification.
    base_url = cfg.get("base_url", "https://www.vinted.be").rstrip("/")
    url = f"{base_url}/catalog?search_text={vt.quote_plus(search.get('query',''))}&order=newest_first"
    if search.get("price_to") is not None:
        url += f"&price_to={float(search['price_to']):g}"
    print(f"\n[SCAN] {search['name']} -> {url}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=12000)
        await page.wait_for_timeout(int(cfg.get("page_wait_ms", 700)))
        await page.locator('a[href*="/items/"]').first.wait_for(timeout=3000)
        cards = await vt.extract_cards(page)
    except Exception as e:
        print(f"  ! VISION rapide indisponible | {str(e)[:80]}")
        return []

    base = charger_base()
    profils_tous = base.get("profils", {})
    ids_autorises = set(search.get("_vision_profils", []))

    for c in cards[:6]:
        if c.get("item_id") in seen_ids:
            continue
        titre_carte = c.get("title", "")
        texte_carte = c.get("text", "")
        prix_carte = vt.parse_price(texte_carte)
        if prix_carte is None or prix_carte <= 1:
            continue

        bloque, _, _, _ = blacklist_check_v69(titre_carte, texte_carte, blacklist)
        if bloque:
            continue
        sane_pack, _ = emballage_seul_multilingue(titre_carte, texte_carte)
        if sane_pack:
            continue

        detail = await vt.verify_listing(page, c.get("url", ""), titre_carte)
        if not detail.get("ok"):
            return []

        titre = detail.get("title") or titre_carte
        texte = detail.get("text") or texte_carte
        image_url = detail.get("image_url") or ""
        # Ici seulement : vision profonde pour retrouver un produit mal décrit.
        emp = _empreinte_visuelle(image_url) if image_url else None
        if emp:
            VISION_PAR_TITRE[n(titre)] = emp
        vis = scores_vision_candidat(titre)
        pid = vis.get("positif_id")
        pos = float(vis.get("positif") or 0)
        neg = float(vis.get("negatif") or 0)
        if not pid or pid not in ids_autorises or pos < 0.93:
            return []
        if neg >= 0.92 and neg > pos + 0.035:
            return []

        profil = profils_tous.get(pid)
        if not profil:
            return []
        rule = recherche_depuis_profil_v77(profil)["rules"][0]
        if not semantique_exemple_ok(rule, titre, texte, deep=True):
            return []

        # Pour éviter de dupliquer tout le moteur de calcul et de créer des
        # alertes fragiles, on ajoute une recherche ciblée courte à la prochaine
        # rotation via la mémoire ; ici on ne force pas d'achat sur la photo seule.
        print(
            f"  🟠 VISION CANDIDAT | {titre[:55]} | photo={pos:.2f} | "
            f"profil={_nettoyer_requete_v77(profil.get('modele',''))[:35]}"
        )
        return []
    return []


async def scan_search_v77(page, search, cfg, blacklist, seen_ids):
    if search.get("_vision_patrouille"):
        return await scan_patrouille_vision_v77(page, search, cfg, blacklist, seen_ids)

    # Nouveau lien positif : on apprend la page exacte et on s'arrête là.
    # Le profil mémorisé sera scanné dans une rotation suivante, ce qui évite
    # d'ouvrir immédiatement une longue recherche de comparables.
    if search.get("_exemple_appris") and not search.get("_profil_db"):
        await enrichir_exemple(page, search)
        return []

    rows = await scan_search_v69(page, search, cfg, blacklist, seen_ids)
    propres = []
    for row in rows or []:
        preparer_triage_row_v77(row)
        if row.get("triage_decision") != "ELIMINER":
            propres.append(row)
    return propres



# ---------------------------------------------------------------------------
# V7.8 - FLASH RECENT : les nouveautés avant les analyses longues
# ---------------------------------------------------------------------------
# Objectif : avec un workflow planifié toutes les ~5 minutes, traiter en premier
# les annonces jamais vues tout en évitant de reparcourir des pages anciennes.
# On n'invente pas d'heure de publication : "récent" signifie ici nouvellement
# découvert dans un flux Vinted trié par newest_first.

import time as _time_v78

V78_DEBUT_RUN = _time_v78.monotonic()
V78_BUDGET_SECONDES = 235
V78_SEEN_STREAK_STOP = 5
V78_FLASH_MAX_CARDS_LARGE = 16
V78_FLASH_MAX_CARDS_CIBLE = 10
V78_FLASH_IDS_DERNIER_SCAN = []
V78_FLASH_ACTIF = False
V78_FLASH_STATS = {"pages": 0, "cartes": 0, "nouvelles": 0, "deja_vues": 0}
V78_FLUX_INITIALISE = False
V78_FLUX_PRECEDENT = set()
V78_FLUX_TOTAL = set()
V78_FLUX_ORDRE = []
_extract_cards_orig_v78 = vt.extract_cards



def _initialiser_flux_v78():
    global V78_FLUX_INITIALISE, V78_FLUX_PRECEDENT, V78_FLUX_TOTAL, V78_FLUX_ORDRE
    if V78_FLUX_INITIALISE:
        return
    base = charger_base()
    etat = base.get('etat_v78', {}) if isinstance(base.get('etat_v78', {}), dict) else {}
    ordre = [str(x) for x in etat.get('flux_ids', []) if str(x)]
    V78_FLUX_ORDRE = ordre[-1200:]
    V78_FLUX_PRECEDENT = set(V78_FLUX_ORDRE)
    V78_FLUX_TOTAL = set(V78_FLUX_ORDRE)
    V78_FLUX_INITIALISE = True
    print(f"[INFO] FLASH mémoire: {len(V78_FLUX_PRECEDENT)} annonce(s) du flux précédent.")


def _memoriser_flux_v78(ids):
    _initialiser_flux_v78()
    change = False
    for iid in ids:
        iid = str(iid or '')
        if not iid:
            continue
        if iid not in V78_FLUX_TOTAL:
            V78_FLUX_ORDRE.append(iid)
            V78_FLUX_TOTAL.add(iid)
            change = True
    if not change:
        return
    if len(V78_FLUX_ORDRE) > 1200:
        V78_FLUX_ORDRE[:] = V78_FLUX_ORDRE[-1200:]
        V78_FLUX_TOTAL.clear()
        V78_FLUX_TOTAL.update(V78_FLUX_ORDRE)
    base = charger_base()
    etat = base.setdefault('etat_v78', {})
    etat['flux_ids'] = list(V78_FLUX_ORDRE)
    etat['dernier_flash'] = datetime.now().isoformat(timespec='seconds')
    base['etat_v78'] = etat
    sauver_base(base)

def _rotation_v78(items, limite, tranche, sel='name'):
    items = [x for x in items if isinstance(x, dict)]
    if len(items) <= limite:
        return items
    items = sorted(items, key=lambda x: n(str(x.get(sel, ''))))
    debut = tranche % len(items)
    tour = items[debut:] + items[:debut]
    return tour[:limite]


def _est_maintenance_v78(s):
    return bool(
        s.get('_exemple_appris')
        or s.get('_rejet_appris')
        or s.get('_rattrapage_profil')
        or s.get('_profil_db')
        or s.get('_vision_patrouille')
    )


def _est_scan_large_v78(s):
    nom = n(str(s.get('name', '')))
    cat = str(s.get('category', ''))
    return (
        'scan large' in nom
        and cat in {'JEU_SWITCH', 'JEU_PS5'}
    )


def _marquer_flash_v78(s, large=False):
    s = dict(s)
    s['_flash_recent'] = True
    s['_priorite_flash'] = 0 if large else 1
    s['max_items'] = min(
        int(s.get('max_items', 15) or 15),
        V78_FLASH_MAX_CARDS_LARGE if large else V78_FLASH_MAX_CARDS_CIBLE,
    )
    return s


def appliquer_exemples_v78(cfg):
    """Construit un cycle court : flux récents -> cibles -> maintenance."""
    nb = appliquer_exemples_v77(cfg)
    recherches = [x for x in cfg.get('searches', []) if isinstance(x, dict)]
    tranche = int(datetime.now().timestamp() // 300)

    grandes = []
    filtres = []
    consoles = []
    jeux_autres = []
    electronique = []
    exemples = []
    rejets = []
    rattr = []
    appris = []
    vision = []
    autres = []

    for s in recherches:
        nom = str(s.get('name', ''))
        cat = str(s.get('category', ''))
        if _est_scan_large_v78(s):
            grandes.append(s)
        elif nom.startswith('FILTRE - '):
            filtres.append(s)
        elif s.get('_exemple_appris') and not s.get('_profil_db'):
            exemples.append(s)
        elif s.get('_rejet_appris'):
            rejets.append(s)
        elif s.get('_rattrapage_profil'):
            rattr.append(s)
        elif s.get('_profil_db'):
            appris.append(s)
        elif s.get('_vision_patrouille'):
            vision.append(s)
        elif cat == 'CONSOLE':
            consoles.append(s)
        elif cat.startswith('JEU_'):
            jeux_autres.append(s)
        elif cat == 'ELECTRONIQUE':
            electronique.append(s)
        else:
            autres.append(s)

    # Les deux grands flux sont toujours premiers : ils couvrent beaucoup de
    # titres avec seulement deux chargements de page.
    flash = [_marquer_flash_v78(s, large=True) for s in grandes[:2]]

    # Les cibles précises tournent. Ainsi un cycle reste court mais toutes les
    # familles reviennent sur quelques passages successifs.
    filtres_sel = _rotation_v78(filtres, 6, tranche * 3 + 1)
    consoles_sel = _rotation_v78(consoles, 5, tranche * 5 + 2)
    jeux_sel = _rotation_v78(jeux_autres, 3, tranche * 7 + 3)
    elec_sel = _rotation_v78(electronique, 2, tranche * 11 + 4)
    autres_sel = _rotation_v78(autres, 1, tranche * 13 + 5)

    flash += [_marquer_flash_v78(s) for s in (filtres_sel + consoles_sel + jeux_sel + elec_sel + autres_sel)]

    # Deux profils appris max par run : ils complètent le flux sans le ralentir.
    appris = appris[:2]

    # Les tâches de mémoire arrivent après les annonces fraîches.
    maintenance = exemples[:1] + rejets[:1] + rattr[:1] + appris
    # La vision large reste occasionnelle et toujours dernière.
    if vision and tranche % 4 == 0:
        maintenance += vision[:1]

    cfg['searches'] = flash + maintenance
    cfg['max_items_per_search'] = min(int(cfg.get('max_items_per_search', 15)), 12)
    cfg['delay_between_searches'] = min(float(cfg.get('delay_between_searches', 0.25)), 0.12)
    cfg['page_wait_ms'] = min(int(cfg.get('page_wait_ms', 700)), 500)

    print(
        f"[INFO] FLASH V7.8: {len(flash)} recherche(s) récentes d'abord, "
        f"{len(maintenance)} tâche(s) secondaire(s)."
    )
    print("[INFO] Priorité: nouvelles annonces détectées depuis le dernier passage (~5 min).")
    return nb


async def _extract_cards_flash_v78(page):
    """Retourne uniquement le haut du flux récent et coupe sur une série déjà vue."""
    global V78_FLASH_IDS_DERNIER_SCAN
    cartes = await _extract_cards_orig_v78(page)
    _initialiser_flux_v78()
    seen = V78_FLUX_TOTAL
    max_cards = int(getattr(_extract_cards_flash_v78, '_max_cards', V78_FLASH_MAX_CARDS_CIBLE))

    selection = []
    consecutifs_vus = 0
    nouveaux = 0
    deja = 0

    for c in cartes[:max_cards]:
        iid = str(c.get('item_id', ''))
        selection.append(c)
        if iid and iid in seen:
            consecutifs_vus += 1
            deja += 1
        else:
            consecutifs_vus = 0
            nouveaux += 1

        # Sur newest_first, 5 anciens consécutifs indiquent généralement qu'on
        # a rejoint la zone du passage précédent. Inutile de descendre plus bas.
        if len(selection) >= 6 and consecutifs_vus >= V78_SEEN_STREAK_STOP:
            break

    V78_FLASH_IDS_DERNIER_SCAN = [str(c.get('item_id', '')) for c in selection if c.get('item_id')]
    V78_FLASH_STATS['pages'] += 1
    V78_FLASH_STATS['cartes'] += len(selection)
    V78_FLASH_STATS['nouvelles'] += nouveaux
    V78_FLASH_STATS['deja_vues'] += deja
    print(
        f"  ⚡ FLASH | {nouveaux} nouvelle(s) potentielle(s) | "
        f"{deja} déjà vue(s) | {len(selection)} carte(s) lue(s)"
    )
    return selection


async def scan_search_v78(page, search, cfg, blacklist, seen_ids):
    global V78_FLASH_ACTIF
    # Les scans récents sont prioritaires et bénéficient d'une coupure rapide
    # dès qu'on retrouve le flux du passage précédent.
    if search.get('_flash_recent'):
        global V78_FLASH_IDS_DERNIER_SCAN
        V78_FLASH_IDS_DERNIER_SCAN = []
        _initialiser_flux_v78()
        _extract_cards_flash_v78._max_cards = int(search.get('max_items', V78_FLASH_MAX_CARDS_CIBLE))
        avant = vt.extract_cards
        vt.extract_cards = _extract_cards_flash_v78
        # Les annonces connues AVANT ce run sont ignorées par le moteur coûteux.
        # Celles découvertes pendant ce run peuvent encore être vues par une
        # recherche plus précise, ce qui évite de rater un titre mal décrit.
        ajoutes_temp = {iid for iid in V78_FLUX_PRECEDENT if iid not in seen_ids}
        seen_ids.update(ajoutes_temp)
        V78_FLASH_ACTIF = True
        try:
            rows = await scan_search_v77(page, search, cfg, blacklist, seen_ids)
        finally:
            V78_FLASH_ACTIF = False
            vt.extract_cards = avant
            # Ne détourne pas annonces_vues.json : la mémoire de flux V7.8 est
            # séparée et persistée dans base_apprentissage.json.
            seen_ids.difference_update(ajoutes_temp)

        _memoriser_flux_v78(V78_FLASH_IDS_DERNIER_SCAN)

        propres = []
        for row in rows or []:
            row['freshness'] = 'NOUVEAU DEPUIS DERNIER SCAN'
            if row.get('triage_decision') != 'ELIMINER':
                propres.append(row)
        return propres

    # Si les annonces fraîches ont déjà utilisé presque tout le budget du run,
    # les recalculs de marché/vision attendent le prochain passage.
    if _time_v78.monotonic() - V78_DEBUT_RUN > V78_BUDGET_SECONDES:
        print(f"[SKIP] budget V7.8 atteint -> {search.get('name','')}")
        return []

    return await scan_search_v77(page, search, cfg, blacklist, seen_ids)


_ntfy_v77_orig_v78 = ntfy_send_v77

def ntfy_send_v78(row):
    if V78_FLASH_ACTIF:
        row['freshness'] = 'NOUVEAU DEPUIS DERNIER SCAN'
    # Pas de faux âge précis : on signale uniquement qu'elle vient d'être
    # découverte dans le flux newest_first depuis le passage précédent.
    if row.get('freshness'):
        ancienne = str(row.get('reason', ''))
        if 'NOUVEAU DEPUIS DERNIER SCAN' not in ancienne:
            row['reason'] = '⚡ NOUVEAU DEPUIS DERNIER SCAN; ' + ancienne
    return _ntfy_v77_orig_v78(row)


# ---------------------------------------------------------------------------
# V7.9 - BANGER EXPRESS : priorité aux annonces susceptibles de partir vite
# ---------------------------------------------------------------------------
# Ce score n'est PAS une probabilité de vente garantie. Il sert uniquement à
# ordonner les nouvelles annonces : prix très bas + forte demande + identité
# claire + faible risque = vérification en premier et notification immédiate.

V79_BANGER_PAR_ID = {}
V79_SCORE_MIN_URGENT = 68


def _score_flash_banger_v79(c, search, blacklist):
    iid = str(c.get("item_id", "") or "")
    titre = str(c.get("title", "") or "")
    texte = str(c.get("text", "") or "")
    categorie = str(search.get("category", "") or "")

    # Une carte du flux précédent n'est jamais prioritaire.
    if iid and iid in V78_FLUX_PRECEDENT:
        return 0, "déjà vue"

    prix = vt.parse_price(texte)
    if prix is None or prix <= 0:
        return 4, "prix non lu"

    # Rejets rapides : uniquement des règles locales/texte, sans ouvrir la page.
    emb, raison_emb = emballage_seul_multilingue(titre, texte)
    if emb:
        return 0, f"emballage seul: {raison_emb}"

    multi, raison_multi = multicart_interdit(titre, texte)
    if multi:
        return 0, raison_multi

    sain, raison_sain = categorie_sanity_v69(categorie, titre)
    if not sain:
        return 0, raison_sain

    try:
        faible, hits_faibles = vt.low_value_game_check(titre, texte, blacklist)
        if faible:
            return 0, "jeu faible valeur"
    except Exception:
        pass

    regle = None
    for r in search.get("rules", []):
        try:
            if rule_match_v69(r, titre, texte, deep=False):
                regle = r
                break
        except Exception:
            continue

    # Les scans larges peuvent montrer des articles non ciblés : ils restent bas.
    if regle is None:
        return 10, "nouvelle annonce mais modèle non confirmé"

    score = 24  # fraîcheur : jamais vue dans le flux précédent
    raisons = ["nouvelle"]

    demande = int(regle.get("demand_score", 3) or 3)
    score += max(0, min(20, demande * 4))
    if demande >= 5:
        raisons.append("forte demande")

    ref = regle.get("market_avg")
    if ref is None:
        ref = regle.get("resale_low")
    try:
        ref = float(ref or 0)
    except Exception:
        ref = 0.0

    ratio = (prix / ref) if ref > 0 else None
    modele = str(regle.get("model") or regle.get("label") or "")

    if categorie == "JEU_SWITCH" and switch_phare(titre, modele):
        if prix <= 5:
            score += 50
            raisons.append("<=5 EUR")
        elif prix <= 10:
            score += 42
            raisons.append("<=10 EUR")
        elif prix <= 15:
            score += 34
            raisons.append("<=15 EUR")
        elif prix <= 18:
            score += 20
            raisons.append("prix encore attractif")
        else:
            score -= 12
    elif categorie == "JEU_PS5":
        if prix <= 10:
            score += 38
            raisons.append("jeu PS5 <=10 EUR")
        elif prix <= 15:
            score += 30
        elif prix <= 20:
            score += 20
        elif ratio is not None and ratio <= 0.45:
            score += 16
    elif categorie == "ELECTRONIQUE" and any(
        x in n(f"{titre} {modele}") for x in ("ti 84", "ti-84", "ti nspire", "ti-nspire")
    ):
        if prix <= 10:
            score += 40
            raisons.append("calculatrice <=10 EUR")
        else:
            score -= 25
    elif categorie == "CONSOLE":
        if ratio is not None:
            if ratio <= 0.12:
                # Trop bas peut être une arnaque : intéressant mais pas BANGER auto.
                score += 12
                raisons.append("prix anormalement bas")
            elif ratio <= 0.28:
                score += 36
                raisons.append("console très sous-cotée")
            elif ratio <= 0.38:
                score += 28
            elif ratio <= 0.50:
                score += 16
            else:
                score -= 12
    elif categorie == "JEU_RETRO":
        # Le rétro reste prudent tant que l'authenticité n'est pas contrôlée.
        if prix <= 10:
            score += 22
        elif prix <= 20:
            score += 12
        if regle.get("authenticity_risk"):
            score -= 10
            raisons.append("authenticité à vérifier")
    elif ratio is not None:
        if ratio <= 0.30:
            score += 30
        elif ratio <= 0.40:
            score += 22
        elif ratio <= 0.50:
            score += 12

    score = int(max(0, min(100, round(score))))
    return score, ", ".join(raisons[:4])


_extract_cards_flash_v78_orig_v79 = _extract_cards_flash_v78

async def _extract_cards_flash_v78(page):
    # Conserve toute la logique de coupure du flux V7.8, puis réordonne seulement
    # les NOUVELLES cartes pour vérifier les bangers avant les annonces moyennes.
    _extract_cards_flash_v78_orig_v79._max_cards = int(
        getattr(_extract_cards_flash_v78, "_max_cards", V78_FLASH_MAX_CARDS_CIBLE)
    )
    cartes = await _extract_cards_flash_v78_orig_v79(page)
    search = getattr(_extract_cards_flash_v78, "_search", {}) or {}
    blacklist = getattr(_extract_cards_flash_v78, "_blacklist", {}) or {}

    classes = []
    for ordre, c in enumerate(cartes):
        score, raison = _score_flash_banger_v79(c, search, blacklist)
        iid = str(c.get("item_id", "") or "")
        if iid:
            V79_BANGER_PAR_ID[iid] = {
                "score_rapide": score,
                "raison_rapide": raison,
                "detecte": datetime.now().isoformat(timespec="seconds"),
            }
        classes.append((c, score, ordre))

    # Score décroissant ; l'ordre newest_first tranche en cas d'égalité.
    classes.sort(key=lambda x: (-x[1], x[2]))
    if classes:
        top = classes[0]
        if top[1] >= V79_SCORE_MIN_URGENT:
            print(
                f"  🔥 PRIORITE BANGER {top[1]}/100 | "
                f"{str(top[0].get('title',''))[:58]}"
            )
    return [x[0] for x in classes]


_verify_listing_v77_orig_v79 = verify_listing_v77

async def verify_listing_v79(page, url, fallback_title=""):
    # En mode FLASH : une seule ouverture détaillée. On ne recharge pas la page
    # uniquement pour récupérer un prix manquant et on ne télécharge pas l'image
    # pour un hash visuel tant que le triage n'en a pas réellement besoin.
    if not V78_FLASH_ACTIF:
        return await _verify_listing_v77_orig_v79(page, url, fallback_title)

    detail = await _ancien_verify_listing(page, url, fallback_title)
    if detail.get("ok"):
        try:
            _enregistrer_observation_vendeur(detail, url)
        except Exception:
            pass
        detail["vision_disponible"] = False
        detail["vision_mode"] = "differee"
        CACHE_DETAILS[str(url).split("?")[0]] = detail
    return detail


def _urgence_finale_v79(row):
    iid = str(row.get("item_id", "") or "")
    quick = int(V79_BANGER_PAR_ID.get(iid, {}).get("score_rapide", 0) or 0)
    ident = int(row.get("triage_identite", 0) or 0)
    rent = int(row.get("triage_rentabilite", 0) or 0)
    risque = int(row.get("triage_risque", 100) or 100)
    demande = int(row.get("demand_score", 3) or 3)

    score = (
        quick * 0.50
        + ident * 0.16
        + rent * 0.19
        + (100 - risque) * 0.10
        + min(100, demande * 20) * 0.05
    )

    cat = str(row.get("category", ""))
    decision = str(row.get("triage_decision", "A EVALUER"))

    # Rétro et annonces à risque ne doivent pas être présentés comme achat certain.
    if cat == "JEU_RETRO":
        score = min(score, 78)
    if risque > 55:
        score = min(score, 74)
    if decision == "ELIMINER":
        score = 0

    return int(max(0, min(100, round(score))))


_scan_search_v78_orig_v79 = scan_search_v78

async def scan_search_v79(page, search, cfg, blacklist, seen_ids):
    if search.get("_flash_recent"):
        _extract_cards_flash_v78._search = search
        _extract_cards_flash_v78._blacklist = blacklist
    rows = await _scan_search_v78_orig_v79(page, search, cfg, blacklist, seen_ids)
    for row in rows or []:
        preparer_triage_row_v77(row)
        urgence = _urgence_finale_v79(row)
        row["urgence_vente"] = urgence
        if urgence >= 88:
            row["urgence_label"] = "BANGER EXPRESS"
        elif urgence >= 78:
            row["urgence_label"] = "TRES URGENT"
        elif urgence >= 68:
            row["urgence_label"] = "A VOIR VITE"
        else:
            row["urgence_label"] = ""
    return rows


def ntfy_send_v79(row):
    preparer_triage_row_v77(row)
    decision = row.get("triage_decision", "A EVALUER")
    if decision == "ELIMINER":
        return False

    urgence = _urgence_finale_v79(row)
    row["urgence_vente"] = urgence
    if urgence >= 88:
        label = "BANGER EXPRESS"
    elif urgence >= 78:
        label = "TRES URGENT"
    elif urgence >= 68:
        label = "A VOIR VITE"
    else:
        label = ""

    topic = str(__import__("os").getenv("NTFY_TOPIC", "")).strip()
    if not topic:
        return False
    server = str(__import__("os").getenv("NTFY_SERVER", "https://ntfy.sh")).rstrip("/")
    endpoint = f"{server}/{urllib.parse.quote(topic, safe='')}"

    prix = float(row.get("listing_price", 0) or 0)
    niveau = ""
    if row.get("category") == "JEU_SWITCH" and switch_phare(row.get("title",""), row.get("model","")):
        if prix <= 5:
            niveau = "A NE PAS RATER"
        elif prix <= 10:
            niveau = "EXCELLENT"
        elif prix <= 15:
            niveau = "BON PRIX"

    # urllib encode les en-têtes HTTP en latin-1. On garde donc le Title
    # volontairement ASCII; les emojis sont rendus via le header Tags.
    if label:
        titre_ntfy = f"{label} | {prix:.2f} EUR"
    else:
        titre_ntfy = (
            ("POSITIF" if decision == "POSITIF" else "A EVALUER")
            + (f" | {niveau}" if niveau else "")
            + f" | {prix:.2f} EUR"
        )
    titre_ntfy = titre_ntfy.encode("ascii", "ignore").decode("ascii")

    rapide = V79_BANGER_PAR_ID.get(str(row.get("item_id", "") or ""), {})
    raison_rapide = str(rapide.get("raison_rapide", "") or "")
    body = (
        f"{row.get('title','')}\n"
        f"Urgence estimée {urgence}/100"
        + (" · estimation de vitesse, pas une garantie de vente" if label else "")
        + "\n"
        f"Identité {row.get('triage_identite')}% | "
        f"Rentabilité {row.get('triage_rentabilite')}% | "
        f"Risque {row.get('triage_risque')}% | "
        f"Marché {row.get('triage_marche')}%\n"
        f"Achat livré estimé {row.get('total_buy_est',0):.2f} EUR | "
        f"Revente prudente {row.get('resale_low',0):.0f}-{row.get('resale_high',0):.0f} EUR | "
        f"Marge prudente {row.get('margin_low',0):+.2f} EUR\n"
        + (f"Signal rapide: {raison_rapide}\n" if raison_rapide else "")
        + f"{row.get('reason','')}\n{row.get('url','')}"
    )

    if urgence >= 88 and decision == "POSITIF":
        priority = "max"
        tags = "fire,rotating_light,moneybag"
    elif urgence >= 75 or decision == "POSITIF":
        priority = "high"
        tags = "zap,moneybag"
    else:
        priority = "default"
        tags = "warning,mag"

    headers = {
        "Title": titre_ntfy,
        "Priority": priority,
        "Tags": tags,
        "Click": str(row.get("url", "")),
        "Actions": f"view, Ouvrir Vinted, {row.get('url','')}",
    }
    if row.get("image_url"):
        headers["Attach"] = str(row.get("image_url"))

    try:
        req = urllib.request.Request(
            endpoint,
            data=body.encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"  ! ntfy V7.10: {e}")
        return False


# ---------------------------------------------------------------------------
# V7.11 - OUVERTURE DE LA PORTE DE TRIAGE
# ---------------------------------------------------------------------------
# Problème corrigé :
# le moteur V6.8 appliquait encore max_buy_ratio / marge / ROI AVANT que
# POSITIF / A EVALUER / ELIMINER puisse examiner la vraie annonce.
# En FLASH récent, on utilise donc une enveloppe d'admission un peu plus large.
# Le triage V7.7 reste la décision finale et continue de bloquer les faux positifs.

def appliquer_exemples_v711(cfg):
    nb = appliquer_exemples_v78(cfg)
    regles_elargies = 0

    for search in cfg.get("searches", []):
        if not isinstance(search, dict) or not search.get("_flash_recent"):
            continue

        cat = str(search.get("category", "") or "")

        for rule in search.get("rules", []):
            if not isinstance(rule, dict):
                continue

            # Garde les seuils normaux comme information de référence.
            rule.setdefault("_v711_ratio_final", rule.get("max_buy_ratio"))
            rule.setdefault("_v711_marge_finale", rule.get("min_margin"))
            rule.setdefault("_v711_roi_final", rule.get("min_roi_pct"))

            try:
                ratio = float(rule.get("max_buy_ratio", 0.40))
            except Exception:
                ratio = 0.40
            try:
                marge = float(rule.get("min_margin", 10))
            except Exception:
                marge = 10.0
            try:
                roi = float(rule.get("min_roi_pct", 20))
            except Exception:
                roi = 20.0

            if cat == "JEU_SWITCH":
                # Permet à un Zelda/Mario/Luigi à 15-18 EUR d'atteindre le triage.
                rule["max_buy_ratio"] = max(ratio, 0.70)
                rule["min_margin"] = min(marge, 3.0)
                rule["min_roi_pct"] = min(roi, 8.0)
            elif cat.startswith("JEU_") and cat != "JEU_RETRO":
                rule["max_buy_ratio"] = max(ratio, 0.70)
                rule["min_margin"] = min(marge, 3.0)
                rule["min_roi_pct"] = min(roi, 8.0)
            elif cat == "JEU_RETRO":
                # Rétro : un peu plus ouvert, mais jamais auto-positif sans contrôle.
                rule["max_buy_ratio"] = max(ratio, 0.62)
                rule["min_margin"] = min(marge, 3.0)
                rule["min_roi_pct"] = min(roi, 8.0)
            elif cat == "CONSOLE":
                rule["max_buy_ratio"] = max(ratio, 0.66)
                rule["min_margin"] = min(marge, 8.0)
                rule["min_roi_pct"] = min(roi, 10.0)
            elif cat == "ELECTRONIQUE":
                rule["max_buy_ratio"] = max(ratio, 0.64)
                rule["min_margin"] = min(marge, 5.0)
                rule["min_roi_pct"] = min(roi, 10.0)

            regles_elargies += 1

    print(
        f"[INFO] V7.11: {regles_elargies} règle(s) FLASH admises jusqu'au triage final."
    )
    return nb


# Le score rapide ne doit pas appeler "BANGER" une mauvaise plateforme explicite.
_score_flash_banger_v79_orig_v711 = _score_flash_banger_v79

def _score_flash_banger_v711(c, search, blacklist):
    score, raison = _score_flash_banger_v79_orig_v711(c, search, blacklist)
    if score <= 0:
        return score, raison

    titre = n(str(c.get("title", "") or ""))
    cat = str(search.get("category", "") or "")

    if cat == "JEU_SWITCH":
        conflits = (
            "3ds", "2ds", "wii u", "wii", "game boy", "gba",
            "ps5", "ps4", "xbox", "vita", "psp"
        )
        if any(present(titre, x) for x in conflits) and not present(titre, "switch"):
            return 0, "mauvaise plateforme pour Switch"

        # Mario Party Island Tour est un titre 3DS, pas Super Mario Party Switch.
        if "mario party island tour" in titre:
            return 0, "Mario Party Island Tour = 3DS"

    elif cat == "JEU_PS5":
        conflits = ("ps4", "ps3", "ps2", "xbox", "switch", "vita", "psp")
        if any(present(titre, x) for x in conflits) and not (
            present(titre, "ps5") or present(titre, "playstation 5")
        ):
            return 0, "mauvaise plateforme pour PS5"

    return score, raison


_score_flash_banger_v79 = _score_flash_banger_v711

# Sauvegarde des fonctions V6.8.
_ancien_blacklist_check = vt.blacklist_check
_ancien_category_sanity = vt.category_sanity_check
_ancien_rule_match = vt.rule_match
_ancien_scan_search = vt.scan_search
_ancien_lire_exemples = vt.lire_exemples
_ancien_verify_listing = vt.verify_listing
_ancien_append_alert = vt.append_alert
_ancien_ntfy_send = vt.ntfy_send

# Transforme les anciennes alertes en HISTORIQUE uniquement.
# Elles ne deviennent jamais automatiquement des exemples positifs.
importer_alertes_csv_existantes()

# Active les correctifs V7.0 dans tout le scanner.
vt.blacklist_check = blacklist_check_v69
vt.category_sanity_check = categorie_sanity_v69
vt.rule_match = rule_match_v69
vt.calibrer_regles_exemple = calibrer_regles_exemple_v77
vt.scan_search = scan_search_v79
vt.appliquer_exemples = appliquer_exemples_v711
vt.verify_listing = verify_listing_v79
vt.append_alert = append_alert_v77
vt.ntfy_send = ntfy_send_v79


if __name__ == "__main__":
    print("Vinted V7.11 — FLASH OUVERT + triage final + BANGER EXPRESS")
    try:
        asyncio.run(vt.main())
    except KeyboardInterrupt:
        print("\nArret demande.")

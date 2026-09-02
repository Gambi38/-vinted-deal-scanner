#!/usr/bin/env python3
# Fusionne les annonces vues et les alertes entre plusieurs passages GitHub Actions.

import csv
import json
import logging
import sys
from pathlib import Path

LOGGER = logging.getLogger("fusion_donnees")


def lire_annonces_vues(chemin):
    fichier = Path(chemin)
    if not fichier.exists():
        return set()

    try:
        donnees = json.loads(fichier.read_text(encoding="utf-8"))
        if not isinstance(donnees, list):
            raise TypeError("la racine JSON doit être une liste")
        return {str(x) for x in donnees if str(x).strip()}
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        LOGGER.warning("État ignoré dans %s: %s", fichier, exc)
        return set()


def lire_alertes(chemin):
    fichier = Path(chemin)

    if not fichier.exists() or fichier.stat().st_size == 0:
        return [], []

    with fichier.open("r", encoding="utf-8-sig", newline="") as f:
        lecteur = csv.DictReader(f)
        lignes = list(lecteur)
        return (lecteur.fieldnames or []), lignes


def fusionner_alertes(distant, local, sortie):
    champs_distants, alertes_distantes = lire_alertes(distant)
    champs_locaux, alertes_locales = lire_alertes(local)

    # Preserve schema additions from either side. The previous implementation
    # selected the remote header and silently dropped new local columns.
    champs = list(dict.fromkeys(champs_distants + champs_locaux))
    if not champs:
        return

    resultat = []
    deja_vu = set()

    for ligne in alertes_distantes + alertes_locales:
        cle = (ligne.get("item_id") or ligne.get("url") or "").strip()

        if cle:
            if cle in deja_vu:
                continue
            deja_vu.add(cle)

        resultat.append(ligne)

    chemin_sortie = Path(sortie)
    chemin_sortie.parent.mkdir(parents=True, exist_ok=True)
    with chemin_sortie.open("w", encoding="utf-8-sig", newline="") as f:
        ecrivain = csv.DictWriter(f, fieldnames=champs)
        ecrivain.writeheader()

        for ligne in resultat:
            ecrivain.writerow({k: ligne.get(k, "") for k in champs})


def main():
    if len(sys.argv) != 5:
        raise SystemExit(
            "Utilisation : fusion_donnees.py "
            "ANNONCES_LOCALES ALERTES_LOCALES "
            "ANNONCES_DISTANTES ALERTES_DISTANTES"
        )

    annonces_locales, alertes_locales, annonces_sortie, alertes_sortie = sys.argv[1:]

    vues_distantes = lire_annonces_vues(annonces_sortie)
    vues_locales = lire_annonces_vues(annonces_locales)

    chemin_annonces_sortie = Path(annonces_sortie)
    chemin_annonces_sortie.parent.mkdir(parents=True, exist_ok=True)
    chemin_annonces_sortie.write_text(
        json.dumps(
            sorted(vues_distantes | vues_locales),
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )

    fusionner_alertes(
        alertes_sortie,
        alertes_locales,
        alertes_sortie,
    )


if __name__ == "__main__":
    main()

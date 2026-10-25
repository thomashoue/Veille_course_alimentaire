"""Export de la veille vers Home Assistant (onglet « Courses » du Green).

La veille du vendredi écrit ``data/ha/veille_courses.json`` ; le Green le lit
toutes les heures (capteur REST) et en tire l'onglet et les alertes. Ce module
ne calcule rien : il vérifie que le fichier respecte le contrat décrit dans
``data/ha/README.md`` avant qu'on le pousse.

    python -m src.export_ha data/ha/veille_courses.json
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ENSEIGNES = ("leclerc", "intermarche", "superu")
QUI = ("maison", "charlotte", "thomas")
NIVEAUX = ("record", "bon", "a_surveiller")
UNITES = ("kg", "L", "unite", "dose", "rouleau", "lavage", "piece")
# Le Green garde le fichier dans les attributs d'un capteur : au-delà, HA
# tronque l'enregistrement et l'onglet devient lent.
TAILLE_MAX = 60_000

_INTERDITS = re.compile(r"carrefour|auchan", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_TELEPHONE = re.compile(r"(?:\+33|0)[1-9](?:[ .-]?\d{2}){4}")


def _date_iso(valeur, chemin: str, erreurs: list[str]) -> None:
    if valeur is None:
        return
    try:
        date.fromisoformat(valeur)
    except (TypeError, ValueError):
        erreurs.append(f"{chemin} : date AAAA-MM-JJ attendue, reçu {valeur!r}")


def _prix(valeur, chemin: str, erreurs: list[str], obligatoire: bool) -> None:
    if valeur is None:
        if obligatoire:
            erreurs.append(f"{chemin} : prix manquant")
        return
    if not isinstance(valeur, (int, float)) or isinstance(valeur, bool) or valeur <= 0:
        erreurs.append(f"{chemin} : nombre > 0 attendu, reçu {valeur!r}")


def verifier(donnees: dict, texte: str) -> list[str]:
    """Renvoie la liste des écarts au contrat (vide = fichier bon à pousser)."""
    erreurs: list[str] = []

    if len(texte.encode("utf-8")) > TAILLE_MAX:
        erreurs.append(f"fichier trop gros ({len(texte.encode('utf-8'))} o > {TAILLE_MAX})")
    if _INTERDITS.search(texte):
        erreurs.append("Carrefour ou Auchan cité : interdit dans la veille")
    if _EMAIL.search(texte) or _TELEPHONE.search(texte):
        erreurs.append("adresse e-mail ou téléphone détecté : aucune donnée personnelle")

    if donnees.get("version") != 1:
        erreurs.append("version : 1 attendu")
    try:
        datetime.fromisoformat(donnees.get("genere_le", ""))
    except (TypeError, ValueError):
        erreurs.append("genere_le : date-heure ISO attendue")
    _date_iso(donnees.get("semaine_du"), "semaine_du", erreurs)
    if not isinstance(donnees.get("resume"), str):
        erreurs.append("resume : texte attendu")

    if set(donnees.get("enseignes", {})) != set(ENSEIGNES):
        erreurs.append(f"enseignes : exactement {', '.join(ENSEIGNES)}")

    comparatif = donnees.get("comparatif")
    if not isinstance(comparatif, list) or not comparatif:
        erreurs.append("comparatif : liste non vide attendue")
        comparatif = []
    for i, ligne in enumerate(comparatif):
        chemin = f"comparatif[{i}]"
        if not ligne.get("produit"):
            erreurs.append(f"{chemin}.produit manquant")
        if ligne.get("unite") not in UNITES:
            erreurs.append(f"{chemin}.unite : une de {', '.join(UNITES)}")
        vus = []
        for e in ENSEIGNES:
            if e not in ligne:
                erreurs.append(f"{chemin}.{e} : clé absente (null si non trouvé)")
                continue
            cellule = ligne[e]
            if cellule is None:
                continue
            _prix(cellule.get("prix"), f"{chemin}.{e}.prix", erreurs, True)
            _prix(cellule.get("pu"), f"{chemin}.{e}.pu", erreurs, False)
            if not isinstance(cellule.get("promo"), bool):
                erreurs.append(f"{chemin}.{e}.promo : true/false attendu")
            _date_iso(cellule.get("fin"), f"{chemin}.{e}.fin", erreurs)
            if cellule.get("pu") is not None:
                vus.append((cellule["pu"], e))
        moins_cher = ligne.get("moins_cher")
        if moins_cher is not None:
            if moins_cher not in ENSEIGNES:
                erreurs.append(f"{chemin}.moins_cher : une des enseignes ou null")
            elif len(vus) >= 2 and min(vus)[1] != moins_cher:
                erreurs.append(f"{chemin}.moins_cher : {min(vus)[1]} a le prix unitaire le plus bas")

    opportunites = donnees.get("opportunites")
    if not isinstance(opportunites, list):
        erreurs.append("opportunites : liste attendue (vide si semaine pauvre)")
        opportunites = []
    for i, offre in enumerate(opportunites):
        chemin = f"opportunites[{i}]"
        for cle in ("titre", "magasin"):
            if not offre.get(cle):
                erreurs.append(f"{chemin}.{cle} manquant")
        if offre.get("qui") not in QUI:
            erreurs.append(f"{chemin}.qui : un de {', '.join(QUI)}")
        if offre.get("niveau") not in NIVEAUX:
            erreurs.append(f"{chemin}.niveau : un de {', '.join(NIVEAUX)}")
        if not isinstance(offre.get("alerte"), bool):
            erreurs.append(f"{chemin}.alerte : true/false attendu")
        _prix(offre.get("prix"), f"{chemin}.prix", erreurs, True)
        _prix(offre.get("pu"), f"{chemin}.pu", erreurs, False)
        _date_iso(offre.get("fin"), f"{chemin}.fin", erreurs)

    return erreurs


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    chemin = Path(argv[0] if argv else "data/ha/veille_courses.json")
    texte = chemin.read_text(encoding="utf-8")
    try:
        donnees = json.loads(texte)
    except json.JSONDecodeError as exc:
        print(f"JSON invalide : {exc}")
        return 1
    erreurs = verifier(donnees, texte)
    for erreur in erreurs:
        print(f"- {erreur}")
    if erreurs:
        print(f"{len(erreurs)} écart(s) : ne pas pousser ce fichier.")
        return 1
    print(f"OK : {len(donnees['comparatif'])} produits, {len(donnees['opportunites'])} opportunités.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

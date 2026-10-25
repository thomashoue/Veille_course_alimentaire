"""Contrat du fichier lu par le Green (data/ha/veille_courses.json)."""

import copy
import json
from pathlib import Path

from src.export_ha import verifier

FICHIER = Path(__file__).resolve().parent.parent / "data" / "ha" / "veille_courses.json"


def _charger():
    texte = FICHIER.read_text(encoding="utf-8")
    return json.loads(texte), texte


def _verifier(donnees):
    return verifier(donnees, json.dumps(donnees, ensure_ascii=False))


def test_fichier_publie_respecte_le_contrat():
    donnees, texte = _charger()
    assert verifier(donnees, texte) == []


def test_enseigne_absente_doit_etre_null_et_non_omise():
    donnees, _ = _charger()
    del donnees["comparatif"][0]["leclerc"]
    assert any("clé absente" in e for e in _verifier(donnees))


def test_moins_cher_incoherent_est_refuse():
    donnees, _ = _charger()
    ligne = donnees["comparatif"][0]
    ligne["leclerc"] = {"prix": 4.80, "pu": 0.80, "promo": False, "fin": None}
    assert any("moins_cher" in e for e in _verifier(donnees))


def test_carrefour_et_donnees_personnelles_refuses():
    donnees, _ = _charger()
    offre = copy.deepcopy(donnees["opportunites"][0])
    offre["magasin"] = "Carrefour Market"
    offre["note"] = "contact : 06 12 34 56 78"
    donnees["opportunites"].append(offre)
    erreurs = _verifier(donnees)
    assert any("Carrefour" in e for e in erreurs)
    assert any("personnelle" in e for e in erreurs)


def test_qui_et_niveau_controles():
    donnees, _ = _charger()
    donnees["opportunites"][0]["qui"] = "voisin"
    donnees["opportunites"][0]["niveau"] = "super"
    erreurs = _verifier(donnees)
    assert any(".qui" in e for e in erreurs)
    assert any(".niveau" in e for e in erreurs)

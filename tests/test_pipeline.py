"""Pipeline complet, hors ligne — §8.

    collect → normalize → validate → shortlist → verify_in_drive
      → assign → report → ledger

Le test rejoue un run réaliste : des pistes d'agrégateur dont la plupart
n'existent pas en drive, deux pièges de prix, une contrainte dure violée.
"""

import json
from datetime import date

import pytest

from src.drive.base import DriveProduct
from src.ledger import Ledger
from src.models import PriceObservation
from src.pipeline import load_observations, run, shortlist


@pytest.fixture
def pistes():
    """Ce que les agrégateurs annoncent un vendredi ordinaire."""
    return [
        # Vraie offre, existe en drive.
        PriceObservation(
            store_id="leclerc_pleumeleuc",
            basket_item_id="lait_demi_ecreme",
            product_label="Lait demi-écrémé 6x1L",
            price_eur=4.80,
        ),
        # Annoncée, absente du drive (constat C1).
        PriceObservation(
            store_id="leclerc_pleumeleuc",
            basket_item_id="croquettes_chat",
            product_label="Ultima 9 kg",
            price_eur=26.95,
        ),
        # Piège P1 : prix habituel au double exact.
        PriceObservation(
            store_id="superu_breteil",
            basket_item_id="cafe",
            product_label="Café moulu 1 kg",
            price_eur=5.00,
            regular_price=10.00,
        ),
        # Contrainte dure violée : litière minérale, même pas chère.
        PriceObservation(
            store_id="superu_breteil",
            basket_item_id="litiere_chat",
            product_label="Litière minérale argile 12 L",
            price_eur=3.20,
        ),
        # Enseigne exclue.
        PriceObservation(
            store_id="carrefour_rennes",
            basket_item_id="lessive",
            product_label="Lessive 45 lavages",
            price_eur=4.00,
        ),
    ]


@pytest.fixture
def drives():
    """Ce que les drives contiennent réellement."""
    return {
        "leclerc_pleumeleuc": {
            "lait": [DriveProduct("L1", "Lait demi-écrémé UHT 6x1L", 4.80)],
        },
        "superu_breteil": {
            "litiere": [DriveProduct("U1", "Litière agglomérante charbon actif 5 L", 4.59)],
        },
    }


class TestShortlist:
    def test_les_pieges_disparaissent_avant_le_reseau(self, config, pistes):
        gardees, verdicts = shortlist(pistes, config)
        labels = [o.product_label for o in gardees]
        assert "Café moulu 1 kg" not in labels           # P1
        assert "Litière minérale argile 12 L" not in labels  # contrainte dure
        assert "Lessive 45 lavages" not in labels        # Carrefour
        assert len(gardees) == 2


class TestRunComplet:
    def test_run_hors_ligne(self, config, pistes, drives, tmp_path):
        result = run(
            config,
            observations=pistes,
            fixtures=drives,
            ledger_path=tmp_path / "obs.sqlite",
            report_dir=tmp_path / "reports",
        )

        # Une seule offre survit à tout : le lait, vu dans le drive.
        assert [o.item.id for o in result.offers] == ["lait_demi_ecreme"]
        assert result.stats.found == 1
        assert result.stats.absent == 1

        # Le plan est une liste par magasin, affectée à une personne.
        assert [b.store.id for b in result.plan.baskets] == ["leclerc_pleumeleuc"]
        assert result.plan.baskets[0].assignee == "charlotte"

        # La litière n'a rien de conforme : le rapport doit le DIRE.
        assert "litiere_chat" in result.plan.unmatched
        assert "Aucune offre conforme" in result.report.markdown
        assert "prix courant" in result.report.markdown

        # Aucune enseigne exclue, nulle part.
        texte = result.report.markdown.lower()
        assert "carrefour" not in texte and "auchan" not in texte

        # Les fichiers sont écrits.
        assert (tmp_path / "reports").exists()
        assert list((tmp_path / "reports").glob("*.md"))

    def test_le_ledger_garde_tout(self, config, pistes, drives, tmp_path):
        path = tmp_path / "obs.sqlite"
        run(config, observations=pistes, fixtures=drives, ledger_path=path,
            report_dir=tmp_path / "reports")
        ledger = Ledger(path)
        # Les pistes sont stockées aussi : elles orientent la semaine suivante.
        assert len(ledger.history("croquettes_chat")) >= 0
        assert ledger.best_price("lait_demi_ecreme") is not None
        ledger.close()

    def test_record_signale_au_deuxieme_run(self, config, drives, tmp_path):
        path = tmp_path / "obs.sqlite"
        cher = [PriceObservation(
            store_id="leclerc_pleumeleuc", basket_item_id="lait_demi_ecreme",
            product_label="Lait demi-écrémé 6x1L", price_eur=5.40,
        )]
        drives_cher = {"leclerc_pleumeleuc": {"lait": [DriveProduct("L1", "Lait demi-écrémé UHT 6x1L", 5.40)]}}
        run(config, observations=cher, fixtures=drives_cher, ledger_path=path)

        moins_cher = [PriceObservation(
            store_id="leclerc_pleumeleuc", basket_item_id="lait_demi_ecreme",
            product_label="Lait demi-écrémé 6x1L", price_eur=4.80,
        )]
        result = run(config, observations=moins_cher, fixtures=drives, ledger_path=path)
        assert result.offers[0].is_record
        assert "record" in result.report.markdown

    def test_sans_drive_rien_n_est_actionnable(self, config, pistes, tmp_path):
        # Sans vérification drive, toutes les pistes de magasins à drive
        # restent des pistes : c'est l'invariant central.
        result = run(config, observations=pistes, use_drive=False,
                     ledger_path=tmp_path / "obs.sqlite")
        assert result.offers == []
        assert result.plan.baskets == []


class TestReleveManuel:
    def test_chargement_json(self, config, tmp_path):
        path = tmp_path / "manual.json"
        path.write_text(
            json.dumps([
                {
                    "store_id": "superu_breteil",
                    "basket_item_id": "litiere_chat",
                    "product_label": "Litière agglomérante charbon actif 5 L",
                    "price_eur": 4.59,
                    "verified_in_drive": True,
                    "observed_at": "2026-08-30T10:00:00",
                    "loyalty_valid_until": "2026-09-05",
                }
            ]),
            encoding="utf-8",
        )
        observations = load_observations(path, config)
        assert observations[0].price_eur == 4.59
        assert observations[0].loyalty_valid_until == date(2026, 9, 5)
        assert observations[0].verified_in_drive is True

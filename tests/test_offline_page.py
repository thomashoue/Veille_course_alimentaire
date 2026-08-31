"""Lecture d'une page de drive enregistrée à la main.

Leclerc Drive bloque les navigateurs pilotés (constaté le 2026-08-31 :
« Accès temporairement restreint »). Cette voie contourne le problème par le
haut : l'humain navigue normalement, le code lit le fichier. L'invariant C1
tient toujours — la page vient bien du drive.
"""

from pathlib import Path

import pytest

from src.drive.offline import extract_products, observations_from_page

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def page_html():
    return (FIXTURES / "drive_search_saved.html").read_text(encoding="utf-8")


@pytest.fixture
def jsonld_html():
    return (FIXTURES / "drive_search_jsonld.html").read_text(encoding="utf-8")


class TestExtraction:
    def test_lecture_par_blocs(self, page_html):
        products, method = extract_products(page_html)
        assert method == "blocs HTML"
        labels = [p.label for p in products]
        assert any("Lait demi-écrémé UHT 6x1L" in label for label in labels)

    def test_le_prix_ne_reste_pas_dans_le_libelle(self, page_html):
        products, _ = extract_products(page_html)
        lait = next(p for p in products if "demi-écrémé UHT" in p.label)
        assert "4,80" not in lait.label
        assert "Ajouter" not in lait.label
        assert lait.price_eur == pytest.approx(4.80)

    def test_indisponible_detecte(self, page_html):
        products, _ = extract_products(page_html)
        bio = next(p for p in products if "bio" in p.label)
        assert bio.available is False

    def test_json_ld_prioritaire(self, jsonld_html):
        products, method = extract_products(jsonld_html)
        assert method == "json-ld"
        assert len(products) == 3


class TestObservations:
    def test_rattachement_au_panier(self, page_html, config):
        store = config.store("leclerc_pleumeleuc")
        observations, report = observations_from_page(page_html, store, config)
        items = {o.basket_item_id for o in observations}
        assert "lait_demi_ecreme" in items
        # Le chocolat n'est pas au panier : il est ignoré, pas une erreur.
        assert report["ignored_not_in_basket"] >= 1

    def test_les_observations_sont_verifiees_en_drive(self, page_html, config):
        store = config.store("leclerc_pleumeleuc")
        observations, _ = observations_from_page(page_html, store, config)
        assert all(o.verified_in_drive for o in observations)
        assert all(o.source == "drive" for o in observations)

    def test_le_format_est_lu(self, page_html, config):
        store = config.store("leclerc_pleumeleuc")
        observations, _ = observations_from_page(page_html, store, config)
        lait = next(o for o in observations if "demi-écrémé UHT" in o.product_label)
        assert (lait.pack_size, lait.pack_unit, lait.pack_count) == (1.0, "l", 6)

    def test_chaine_complete_depuis_une_page_enregistree(self, jsonld_html, config, tmp_path):
        """De la page enregistrée au rapport, sans réseau ni navigateur."""
        from src.pipeline import run

        store = config.store("leclerc_pleumeleuc")
        observations, _ = observations_from_page(jsonld_html, store, config)
        result = run(
            config,
            observations=observations,
            use_drive=False,          # déjà vérifiées : elles viennent du drive
            ledger_path=tmp_path / "obs.sqlite",
        )
        offres = {o.item.id for o in result.offers}
        assert "croquettes_chat" in offres
        assert "litiere_chat" in offres
        # La litière minérale est écartée par la contrainte dure, pas par le prix.
        labels = [o.observation.product_label for o in result.offers]
        assert not any("minérale" in label for label in labels)

"""Contraintes dures : la conformité produit précède le prix (§2.2)."""

import pytest

from src.models import Status
from src.validate import ConstraintError, check_constraint, validate


class TestDSL:
    def test_in(self):
        assert check_constraint("type in (silice, agglo_charbon)", {"type": "silice"}).ok

    def test_not_in(self):
        assert check_constraint("format not in (spray)", {"format": "bidon"}).ok
        assert check_constraint("format not in (spray)", {"format": "spray"}).rejected

    def test_attribut_inconnu_ne_passe_jamais_en_ok(self):
        # Un produit « peut-être conforme » n'est pas conforme.
        verdict = check_constraint("type in (silice)", {})
        assert verdict.status is Status.FLAG

    def test_contrainte_illisible_est_un_bug_de_config(self):
        with pytest.raises(ConstraintError):
            check_constraint("n'importe quoi", {"type": "silice"})


class TestLitiere:
    """Trois semaines sans promo conforme : le code doit savoir dire non."""

    @pytest.mark.parametrize(
        "label, attendu",
        [
            ("Litière chat cristaux de silice 5 L", "silice"),
            ("Litière agglomérante au charbon actif 10 L", "agglo_charbon"),
            ("Litière minérale argile 12 L", "minerale"),
            ("Litière végétale bois 20 L", "vegetale"),
        ],
    )
    def test_detection_du_type(self, observe, label, attendu):
        obs = observe(basket_item_id="litiere_chat", product_label=label, price_eur=5.0)
        assert obs.attributes.get("type") == attendu

    def test_bentonite_au_charbon_actif_est_conforme(self, observe, config):
        # L'ordre des règles compte : « charbon actif » doit gagner sur « bentonite ».
        obs = observe(
            basket_item_id="litiere_chat",
            product_label="Litière bentonite agglomérante au charbon actif 10 L",
            price_eur=8.90,
        )
        assert obs.attributes["type"] == "agglo_charbon"
        assert validate(obs, config).status is not Status.REJECT

    def test_litiere_minerale_tres_bon_marche_reste_hors_sujet(self, observe, config):
        obs = observe(
            basket_item_id="litiere_chat",
            product_label="Litière minérale argile 20 L",
            price_eur=2.00,       # 0,10 €/L : imbattable, et hors sujet
            verified_in_drive=True,
        )
        verdict = validate(obs, config)
        assert verdict.status is Status.REJECT
        assert "C-HARD" in verdict.rules


class TestVinaigre:
    def test_le_spray_est_refuse(self, observe, config):
        obs = observe(
            basket_item_id="vinaigre",
            product_label="Vinaigre ménager spray 750 ml",
            price_eur=2.10,
        )
        assert validate(obs, config).status is Status.REJECT

    def test_le_bidon_passe(self, observe, config):
        obs = observe(
            basket_item_id="vinaigre",
            product_label="Vinaigre blanc bidon 5 L",
            price_eur=4.50,
        )
        assert validate(obs, config).status is not Status.REJECT
        assert obs.unit_price == pytest.approx(0.90)

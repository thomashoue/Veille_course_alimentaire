"""Les contre-exemples réels du §5 comme fixtures.

Chaque test correspond à un piège constaté pendant les trois semaines
d'expérimentation. Si l'un d'eux casse, c'est qu'on a réintroduit une erreur
qui avait déjà coûté de l'argent.
"""

from datetime import date, timedelta

import pytest

from src.models import Status
from src.validate import (
    grade,
    is_reportable,
    p1_exact_double,
    p2_absurd_ratio,
    p8_worth_detour,
    validate,
)
from src.models import Grade


class TestP1DoubleExact:
    """Ratio exactement 2,00 → mécanique 2ᵉ article, jamais une remise."""

    def test_double_exact_rejete(self, observe):
        obs = observe(
            basket_item_id="croquettes_chat",
            product_label="Croquettes chat 2 kg",
            price_eur=4.50,
            regular_price=9.00,
        )
        assert p1_exact_double(obs).status is Status.REJECT

    def test_tolerance_serree(self, observe):
        # 1,98x : c'est une vraie remise de 49,5 %, elle doit passer.
        obs = observe(
            basket_item_id="croquettes_chat",
            product_label="Croquettes chat 2 kg",
            price_eur=4.50,
            regular_price=8.91,
        )
        assert p1_exact_double(obs).status is Status.OK

    def test_sans_prix_habituel_pas_de_verdict(self, observe):
        obs = observe(
            basket_item_id="croquettes_chat",
            product_label="Croquettes chat 2 kg",
            price_eur=4.50,
        )
        assert p1_exact_double(obs).status is Status.OK


class TestP2RatioAberrant:
    """Ratio > 2,4 : l'agrégateur raconte n'importe quoi."""

    def test_ratio_aberrant_rejete(self, observe):
        obs = observe(
            basket_item_id="lessive",
            product_label="Lessive 45 lavages",
            price_eur=4.00,
            regular_price=12.00,
        )
        assert p2_absurd_ratio(obs).status is Status.REJECT

    def test_limite_2_4_acceptee(self, observe):
        obs = observe(
            basket_item_id="lessive",
            product_label="Lessive 45 lavages",
            price_eur=5.00,
            regular_price=12.00,
        )
        assert p2_absurd_ratio(obs).status is Status.OK


class TestP3MoyenneSurQuantite:
    """4,43 € le 1er sac + 3,10 € le 2ᵉ = 0,377 €/L, pas 0,31 €/L."""

    def test_contre_exemple_vecu(self, observe):
        obs = observe(
            basket_item_id="litiere_chat",
            product_label="Litière silice 10 L",
            price_eur=4.43,
            mechanic="second_-30",
        )
        assert obs.unit_price == pytest.approx(0.443)
        assert obs.effective_unit_price == pytest.approx(0.3765, abs=1e-4)
        # Le prix que l'agrégateur affichait — celui qu'il ne faut jamais retenir.
        assert obs.effective_unit_price > 0.31

    def test_second_a_moitie_prix(self, observe):
        obs = observe(
            basket_item_id="lait_demi_ecreme",
            product_label="Lait demi-écrémé 6x1L",
            price_eur=6.00,
            mechanic="second_-50",
        )
        assert obs.effective_unit_price == pytest.approx(0.75)

    def test_trois_pour_deux(self, observe):
        obs = observe(
            basket_item_id="conserve_poisson",
            product_label="Sardines 3x93g",
            price_eur=3.00,
            mechanic="3_pour_2",
            weight_basis="net_egoutte",
        )
        # 2 payés pour 3 pris : 6 € pour 0,837 kg.
        assert obs.effective_unit_price == pytest.approx(6.0 / (3 * 0.279), abs=1e-3)

    def test_le_prix_retenu_est_l_effectif(self, observe):
        obs = observe(
            basket_item_id="litiere_chat",
            product_label="Litière silice 10 L",
            price_eur=4.43,
            mechanic="second_-30",
        )
        assert obs.best_unit_price == obs.effective_unit_price


class TestP4FormatAbsent:
    """Grammage absent → interdiction de calculer un €/kg."""

    def test_pas_de_prix_au_kilo_sans_format(self, observe, config):
        obs = observe(
            basket_item_id="steak_hache",
            product_label="Steak haché Charal promo",
            price_eur=4.99,
        )
        assert obs.unit_price is None
        verdict = validate(obs, config)
        assert verdict.status is Status.FLAG
        assert "P4" in verdict.rules


class TestP5BaseDePoids:
    """Brut vs net égoutté : Leclerc annonce en brut, Intermarché en net."""

    def test_base_inconnue_flaggee(self, observe, config):
        obs = observe(
            basket_item_id="conserve_poisson",
            product_label="Sardines à l'huile 140 g",
            price_eur=1.55,
        )
        verdict = validate(obs, config)
        assert verdict.status is Status.FLAG
        assert "P5" in verdict.rules

    def test_brut_converti_en_net_egoutte(self, observe, config):
        brut = observe(
            basket_item_id="conserve_poisson",
            product_label="Sardines 140 g",
            price_eur=1.55,
            weight_basis="brut",
        )
        net = observe(
            basket_item_id="conserve_poisson",
            product_label="Sardines 140 g",
            price_eur=1.55,
            weight_basis="net_egoutte",
        )
        # À prix et grammage identiques, le brut est plus cher au kilo réel.
        assert brut.unit_price > net.unit_price
        assert validate(brut, config).status is not Status.REJECT


class TestP6AvantageCarte:
    """L'avantage carte dépend de la date de RETRAIT, pas de commande."""

    def test_retrait_dans_la_fenetre(self, observe):
        obs = observe(
            basket_item_id="cafe",
            product_label="Café moulu 1 kg",
            price_eur=10.00,
            loyalty_pct=20,
            loyalty_valid_until=date(2026, 9, 5),
            pickup_date=date(2026, 9, 3),
        )
        assert obs.effective_unit_price == pytest.approx(8.00)

    def test_retrait_hors_fenetre_annule_l_avantage(self, observe, config):
        pickup = date(2026, 9, 10)
        obs = observe(
            basket_item_id="cafe",
            product_label="Café moulu 1 kg",
            price_eur=10.00,
            loyalty_pct=20,
            loyalty_valid_until=date(2026, 9, 5),
            pickup_date=pickup,
        )
        assert obs.effective_unit_price == pytest.approx(10.00)
        assert obs.loyalty_pct == 0
        verdict = validate(obs, config, pickup_date=pickup)
        assert "P6" in verdict.rules


class TestP7PetitFormat:
    """125 g à −30 % = 11,52 €/kg contre un 500 g plein tarif à 6,78 €/kg."""

    def test_le_petit_format_en_promo_perd(self, observe):
        petit = observe(
            basket_item_id="emmental_rape",
            product_label="Emmental râpé 125 g",
            price_eur=1.44,
        )
        grand = observe(
            basket_item_id="emmental_rape",
            product_label="Emmental râpé 500 g",
            price_eur=3.39,
        )
        assert petit.unit_price == pytest.approx(11.52)
        assert grand.unit_price == pytest.approx(6.78)
        assert petit.unit_price > grand.unit_price

    def test_promo_trompeuse_signalee(self, observe, config):
        obs = observe(
            basket_item_id="emmental_rape",
            product_label="Emmental râpé 125 g",
            price_eur=1.44,
            mechanic="second_-30",
        )
        verdict = validate(obs, config)
        assert "P7" in verdict.rules


class TestP8CoutDuDetour:
    """~2,50 € d'économie pour ~25 km ≈ le carburant : ça ne vaut pas le détour."""

    def test_arbitrage_calibre(self, config):
        worth, net = p8_worth_detour(2.50, 25, config)
        assert not worth
        assert net == pytest.approx(0.0)

    def test_detour_court_rentable(self, config):
        worth, net = p8_worth_detour(2.50, 2, config)
        assert worth
        assert net == pytest.approx(2.30)

    def test_grosse_economie_justifie_le_detour(self, config):
        worth, _ = p8_worth_detour(12.00, 25, config)
        assert worth


class TestC1VerificationDrive:
    """Le catalogue n'est pas l'assortiment du drive."""

    @pytest.mark.parametrize(
        "label, price",
        [
            ("Prince 1,2 kg", 2.51),
            ("Friskies 2 kg", 4.22),
            ("Ultima 9 kg", 26.95),
            ("Bigard 640 g", 9.99),
        ],
    )
    def test_offres_absentes_du_drive_non_actionnables(self, observe, config, label, price):
        # Les quatre offres du constat C1 : annoncées, introuvables en drive.
        obs = observe(
            basket_item_id="croquettes_chat",
            product_label=label,
            price_eur=price,
            verified_in_drive=False,
        )
        verdict = validate(obs, config)
        assert verdict.status is Status.FLAG
        assert "C1" in verdict.rules
        assert not is_reportable(verdict, obs)

    def test_enseigne_sans_drive_reste_actionnable(self, observe, config):
        # Action n'a pas de drive : son prix permanent est bien le prix payé.
        obs = observe(
            store_id="action_tregueux",
            basket_item_id="papier_toilette",
            product_label="Papier toilette 24 rouleaux",
            price_eur=4.97,
            verified_in_drive=False,
        )
        assert obs.unit_price == pytest.approx(0.207, abs=1e-3)
        assert is_reportable(validate(obs, config), obs)


class TestExclusionsDures:
    """Carrefour et Auchan : jamais, nulle part."""

    def test_offre_carrefour_rejetee(self, observe, config):
        obs = observe(
            store_id="carrefour_rennes",
            basket_item_id="lait_demi_ecreme",
            product_label="Lait demi-écrémé 6x1L",
            price_eur=3.00,
        )
        verdict = validate(obs, config)
        assert verdict.status is Status.REJECT
        assert "X-BANNER" in verdict.rules

    def test_offre_auchan_rejetee(self, observe, config):
        obs = observe(
            store_id="auchan_saint_brieuc",
            basket_item_id="lait_demi_ecreme",
            product_label="Lait demi-écrémé 6x1L",
            price_eur=2.90,
        )
        assert validate(obs, config).status is Status.REJECT


class TestSeuils:
    """§4 — les seuils calibrés, y compris les cas qui ne sont PAS des affaires."""

    def test_lait_sous_le_seuil_de_stockage(self, observe, config):
        obs = observe(
            basket_item_id="lait_demi_ecreme",
            product_label="Lait demi-écrémé 6x1L",
            price_eur=4.50,
        )
        assert grade(obs, config) is Grade.STOCK

    def test_lait_au_dessus_du_plafond(self, observe, config):
        obs = observe(
            basket_item_id="lait_demi_ecreme",
            product_label="Lait demi-écrémé 6x1L",
            price_eur=6.90,
        )
        assert grade(obs, config) is Grade.TOO_HIGH

    def test_comte_dans_la_fourchette_normale_n_est_pas_une_promo(self, observe, config):
        obs = observe(
            basket_item_id="comte",
            product_label="Comté 6 mois 250 g",
            price_eur=4.75,          # 19 €/kg : prix courant, pas une trouvaille
        )
        assert grade(obs, config) is Grade.NORMAL

    def test_papier_toilette_action(self, observe, config):
        obs = observe(
            store_id="action_tregueux",
            basket_item_id="papier_toilette",
            product_label="Papier toilette 24 rouleaux",
            price_eur=4.97,
        )
        assert grade(obs, config) is Grade.GOOD

    def test_litiere_charbon_reference_u(self, observe, config):
        obs = observe(
            store_id="superu_breteil",
            basket_item_id="litiere_chat",
            product_label="Litière agglomérante charbon actif 5 L",
            price_eur=4.59,
        )
        assert obs.unit_price == pytest.approx(0.918)
        assert grade(obs, config) is Grade.GOOD

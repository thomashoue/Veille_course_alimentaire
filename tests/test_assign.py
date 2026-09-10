"""Affectation par corridor et arbitrage du détour (§2.1, P8)."""

import pytest

from src.assign import ExcludedBannerLeak, Plan, StoreBasket, assert_no_excluded, assign
from src.models import OK, Grade, Offer


def make_offer(config, store_id, item_id, label, price, saving=1.0, verified=True):
    from src.models import PriceObservation
    from src.normalize import normalize

    obs = PriceObservation(
        store_id=store_id,
        basket_item_id=item_id,
        product_label=label,
        price_eur=price,
        verified_in_drive=verified,
    )
    normalize(obs, config)
    return Offer(
        observation=obs,
        item=config.item(item_id),
        verdict=OK(),
        grade=Grade.GOOD,
        saving_eur=saving,
    )


class TestAffectation:
    def test_chaque_magasin_va_a_la_bonne_personne(self, config):
        offers = [
            make_offer(config, "leclerc_pleumeleuc", "lait_demi_ecreme", "Lait 6x1L", 4.80, 2.0),
            make_offer(config, "hyperu_yffiniac", "cafe", "Café 1 kg", 8.00, 3.0),
            make_offer(config, "intermarche_montauban", "pates", "Pâtes 1 kg", 1.20, 1.0),
        ]
        plan = assign(offers, config)
        par_personne = {b.store.id: b.assignee for b in plan.baskets}
        assert par_personne["leclerc_pleumeleuc"] == "charlotte"
        assert par_personne["hyperu_yffiniac"] == "thomas"
        assert par_personne["intermarche_montauban"] == "household"

    def test_le_meilleur_prix_gagne(self, config):
        cher = make_offer(config, "leclerc_pleumeleuc", "lait_demi_ecreme", "Lait 6x1L", 5.40, 1.0)
        pas_cher = make_offer(config, "superu_breteil", "lait_demi_ecreme", "Lait 6x1L", 4.80, 2.0)
        plan = assign([cher, pas_cher], config)
        assert [b.store.id for b in plan.baskets] == ["superu_breteil"]

    def test_domicile_ne_se_discute_pas(self, config):
        # 0 km de détour : même une économie ridicule reste bonne à prendre.
        offer = make_offer(config, "intermarche_montauban", "pates", "Pâtes 1 kg", 1.20, 0.05)
        plan = assign([offer], config)
        assert [b.store.id for b in plan.baskets] == ["intermarche_montauban"]


class TestGainReel:
    """La ligne « gain réel » : meilleur magasin unique vs éclaté."""

    def _offer(self, config, store_id, item_id, price, pack_kg):
        from src.models import PriceObservation
        from src.normalize import normalize
        obs = PriceObservation(
            store_id=store_id, basket_item_id=item_id, product_label=f"{item_id} {pack_kg}kg",
            price_eur=price, pack_size=pack_kg, pack_unit="kg", pack_count=1,
            verified_in_drive=True,
        )
        normalize(obs, config)
        return Offer(observation=obs, item=config.item(item_id), verdict=OK(),
                     grade=Grade.GOOD, saving_eur=0.0)

    def test_gain_eclatement_vs_magasin_unique(self, config):
        # riz : Leclerc 3 €/kg, Hyper U 4 €/kg  → Leclerc gagne (qty 1 kg)
        # pâtes : Leclerc 5 €/kg, Hyper U 2 €/kg → Hyper U gagne (qty 2 kg)
        offers = [
            self._offer(config, "leclerc_pleumeleuc", "riz", 3.0, 1.0),
            self._offer(config, "hyperu_yffiniac", "riz", 4.0, 1.0),
            self._offer(config, "leclerc_pleumeleuc", "pates", 5.0, 1.0),
            self._offer(config, "hyperu_yffiniac", "pates", 2.0, 1.0),
        ]
        cost = assign(offers, config).cost
        assert cost is not None and cost.n_items == 2
        # éclaté : riz 3×1 + pâtes 2×2 = 7 ; meilleur unique = Hyper U (4 + 2×2 = 8)
        assert cost.split_total == pytest.approx(7.0)
        assert cost.best_single_store == "hyperu_yffiniac"
        assert cost.best_single_total == pytest.approx(8.0)
        assert cost.real_gain == pytest.approx(1.0)

    def test_un_seul_magasin_aucun_gain(self, config):
        offers = [self._offer(config, "intermarche_montauban", "riz", 2.0, 1.0)]
        cost = assign(offers, config).cost
        assert cost.real_gain == pytest.approx(0.0)
        assert cost.best_single_covers_all is True


class TestDetour:
    def test_petit_gain_ne_justifie_pas_le_detour(self, config):
        offer = make_offer(config, "action_pace", "papier_toilette", "PQ 24 rouleaux", 4.97, 0.10)
        plan = assign([offer], config)
        assert plan.baskets == []
        assert [b.store.id for b in plan.dropped] == ["action_pace"]
        assert "non amorti" in plan.dropped[0].drop_reason

    def test_repli_sur_le_deuxieme_meilleur_magasin(self, config):
        # Le magasin le mieux placé est écarté pour cause de détour :
        # l'article doit se replier, pas disparaître.
        detour = make_offer(config, "action_pace", "lessive", "Lessive 45 lavages", 8.00, 0.10)
        proche = make_offer(config, "leclerc_pleumeleuc", "lessive", "Lessive 45 lavages", 8.50, 3.0)
        plan = assign([detour, proche], config)
        assert [b.store.id for b in plan.baskets] == ["leclerc_pleumeleuc"]

    def test_gain_net_calcule(self, config):
        offer = make_offer(config, "leclerc_ploufragan", "cafe", "Café 1 kg", 7.00, 5.0)
        plan = assign([offer], config)
        basket = plan.baskets[0]
        assert basket.detour_cost_eur == pytest.approx(0.20)   # 2 km x 0,10 €
        assert basket.net_gain_eur == pytest.approx(4.80)


class TestInvariants:
    def test_les_pistes_n_entrent_pas_dans_le_plan(self, config):
        piste = make_offer(
            config, "leclerc_pleumeleuc", "lait_demi_ecreme", "Lait 6x1L", 3.00,
            saving=5.0, verified=False,
        )
        plan = assign([piste], config)
        assert plan.baskets == []
        assert "lait_demi_ecreme" in plan.unmatched

    def test_enseigne_exclue_filtree_en_entree(self, config):
        offer = make_offer(config, "carrefour_rennes", "lait_demi_ecreme", "Lait 6x1L", 2.00, 9.0)
        plan = assign([offer], config)
        assert plan.baskets == []

    def test_assertion_de_sortie(self, config):
        # Si une enseigne exclue arrivait malgré tout en sortie, c'est un bug :
        # l'assertion doit exploser, pas laisser passer.
        plan = Plan(baskets=[StoreBasket(store=config.store("carrefour_rennes"))])
        with pytest.raises(ExcludedBannerLeak):
            assert_no_excluded(plan, config)

    def test_fruits_et_legumes_hors_perimetre(self, config):
        plan = assign([], config)
        assert "tomate" in plan.out_of_scope
        assert "tomate" not in plan.unmatched


class TestOffreReportee:
    """Une offre conforme dans un magasin écarté n'est pas une absence d'offre."""

    def test_article_du_magasin_ecarte_est_reporte_pas_perdu(self, config):
        offer = make_offer(config, "superu_breteil", "litiere_chat",
                           "Litière charbon actif 5 L", 4.59, saving=0.15)
        plan = assign([offer], config)
        assert plan.baskets == []
        assert "litiere_chat" in plan.deferred
        assert "litiere_chat" not in plan.unmatched

    def test_article_sans_aucune_offre_reste_non_couvert(self, config):
        plan = assign([], config)
        assert "litiere_chat" in plan.unmatched
        assert plan.deferred == {}

    def test_raison_du_rejet_mentionne_le_minimum(self, config):
        offer = make_offer(config, "superu_breteil", "litiere_chat",
                           "Litière charbon actif 5 L", 4.59, saving=0.15)
        plan = assign([offer], config)
        assert "minimum" in plan.dropped[0].drop_reason


class TestCoutOpportunite:
    """Le détour se juge sur ce qu'on perdrait ailleurs, pas sur l'écart au seuil.

    Bug révélé par un test 3 enseignes : le café (sans seuil) était 1,20 € moins
    cher chez U, ce qui amortit largement 3 km, mais l'ancien calcul fermait U
    parce que son « économie vs seuil » était nulle.
    """

    def test_un_magasin_sans_affaire_mais_moins_cher_vaut_le_detour(self, config):
        # Café sans seuil : 8,40 € chez U (détour), 9,60 € chez Leclerc (sur trajet).
        u = make_offer(config, "hyperu_yffiniac", "cafe", "Café U 1kg", 8.40, saving=0.0)
        leclerc = make_offer(config, "leclerc_pleumeleuc", "cafe", "Café 1kg", 9.60, saving=0.0)
        plan = assign([u, leclerc], config)
        retenus = {b.store.id for b in plan.baskets}
        assert "hyperu_yffiniac" in retenus       # 1,20 € > coût de 3 km

    def test_ecart_trop_faible_ne_justifie_pas(self, config):
        # 10 centimes d'écart sur un article : le détour de 3 km ne se paie pas.
        u = make_offer(config, "hyperu_yffiniac", "cafe", "Café U 1kg", 9.50, saving=0.0)
        leclerc = make_offer(config, "leclerc_pleumeleuc", "cafe", "Café 1kg", 9.60, saving=0.0)
        plan = assign([u, leclerc], config)
        assert "hyperu_yffiniac" not in {b.store.id for b in plan.baskets}

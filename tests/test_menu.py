"""Menu de la semaine et liste de courses par besoin."""

import pytest

from src.menu import plan_week, shopping_list


class TestPlanWeek:
    def test_sept_diners_sans_repetition(self, config):
        week = plan_week(config, seed=3, avoid_recent=False)
        assert len(week) == 7
        assert len({r.id for r in week}) == 7        # tous différents

    def test_poisson_plafonne(self, config):
        for seed in range(6):
            week = plan_week(config, seed=seed, avoid_recent=False, fish_max=2)
            fish = sum(1 for r in week if r.proteine == "poisson")
            assert fish <= 2

    def test_reproductible_par_seed(self, config):
        a = [r.id for r in plan_week(config, seed=42, avoid_recent=False)]
        b = [r.id for r in plan_week(config, seed=42, avoid_recent=False)]
        assert a == b


class TestShoppingList:
    def test_ingredients_agreges(self, config):
        week = plan_week(config, seed=1, avoid_recent=False)
        menu = shopping_list(config, week, servings=4)
        # Trois listes distinctes, non vides sur une semaine complète.
        assert menu.to_buy and menu.fresh
        # Chaque ligne « à acheter » est bien reliée à un article du panier.
        assert all(l.basket_item in config.items for l in menu.to_buy)

    def test_mise_a_l_echelle_des_convives(self, config):
        # Une recette connue, quantités doublées de 4 à 8 parts.
        from src.models import Recipe

        dahl = config.recipe("dahl_lentilles_corail")
        m4 = shopping_list(config, [dahl], servings=4)
        m8 = shopping_list(config, [dahl], servings=8)
        riz4 = next(l.qty for l in m4.to_buy if l.basket_item == "riz")
        riz8 = next(l.qty for l in m8.to_buy if l.basket_item == "riz")
        assert riz8 == pytest.approx(riz4 * 2)

    def test_frais_hors_veille_prix(self, config):
        week = plan_week(config, seed=2, avoid_recent=False)
        menu = shopping_list(config, week)
        # Les fruits/légumes et le poisson frais ne vont pas dans « à acheter drive ».
        assert all(l.category not in ("fl", "poisson") for l in menu.to_buy)


class TestStock:
    """Inventaire : besoin − stock, départ à zéro."""

    def test_stock_couvre_le_besoin(self, config, tmp_path):
        from src.inventory import Inventory
        from src.menu import plan_week, shopping_list

        week = plan_week(config, seed=1, avoid_recent=False)
        base = shopping_list(config, week)
        # On met en stock de quoi couvrir entièrement le premier article.
        item = base.to_buy[0]
        stock = {(item.basket_item, item.unit): item.need}
        with_stock = shopping_list(config, week, stock=stock)
        assert all(l.basket_item != item.basket_item for l in with_stock.to_buy)
        assert any(l.basket_item == item.basket_item for l in with_stock.covered)

    def test_stock_partiel_reduit_la_quantite(self, config):
        from src.menu import plan_week, shopping_list

        week = plan_week(config, seed=1, avoid_recent=False)
        base = shopping_list(config, week)
        item = base.to_buy[0]
        stock = {(item.basket_item, item.unit): item.need / 2}
        reduced = next(l for l in shopping_list(config, week, stock=stock).to_buy
                       if l.basket_item == item.basket_item)
        assert reduced.qty == pytest.approx(item.need / 2, abs=0.02)

    def test_acheter_puis_consommer(self, config, tmp_path):
        from src.inventory import Inventory, consume_menu

        inv = Inventory(tmp_path / "inv.json")
        inv.add("riz", 2, "kg")
        consume_menu(inv, config, [config.recipe("dahl_lentilles_corail")])
        # Le dahl consomme 0,25 kg de riz : il en reste 1,75.
        assert inv.current()[("riz", "kg")] == pytest.approx(1.75)

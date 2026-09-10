"""Page « Menu de la semaine » : données injectées et validation qui écrit."""

import json

import pytest

from src import inventory as inventory_mod
from src import menu as menu_mod
from src import webmenu
from src.inventory import Inventory
from src.webmenu import build_menu_data, render_page, save_menu_choice


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirige les écritures d'état vers un dossier jetable."""
    monkeypatch.setattr(menu_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(inventory_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(webmenu, "DATA_DIR", tmp_path)
    return tmp_path


class TestBuildData:
    def test_expose_recettes_et_destinataires(self, config):
        data = build_menu_data(config)
        assert data["servings_base"] == config.servings_base
        assert len(data["recipes"]) == len(config.recipes)
        assert data["recipients"]                      # non vide
        first = data["recipes"][0]
        assert {"id", "name", "tags", "proteine", "ingredients"} <= first.keys()

    def test_rendu_injecte_les_donnees(self, config):
        page = render_page(config, '<script id="menu-data" type="application/json">OLD</script>')
        assert "OLD" not in page
        assert '"recipes"' in page and '"recipients"' in page


class TestSaveChoice:
    def test_ecrit_menu_courant(self, config, data_dir):
        summary = save_menu_choice(config, ["dahl_lentilles_corail", "omelette_pdt"], servings=4)
        saved = json.loads((data_dir / "menu_courant.json").read_text(encoding="utf-8"))
        assert [m["id"] for m in saved["menu"]] == ["dahl_lentilles_corail", "omelette_pdt"]
        assert summary["servings"] == 4
        assert saved["to_buy"]                          # au moins un article à acheter

    def test_decompte_le_stock(self, config, data_dir):
        save_menu_choice(config, ["dahl_lentilles_corail"], servings=4, cook=True)
        stock = Inventory().current()
        # le riz de la recette est décompté (hypothèse de départ : zéro → négatif)
        assert stock[("riz", "kg")] < 0

    def test_memorise_la_semaine(self, config, data_dir):
        save_menu_choice(config, ["dahl_lentilles_corail"], servings=4)
        hist = json.loads((data_dir / "menu_history.json").read_text(encoding="utf-8"))
        assert hist[-1]["recipes"] == ["dahl_lentilles_corail"]

    def test_recette_inconnue_rejetee(self, config, data_dir):
        with pytest.raises(ValueError):
            save_menu_choice(config, ["pas_une_recette"])

    def test_selection_vide_rejetee(self, config, data_dir):
        with pytest.raises(ValueError):
            save_menu_choice(config, [])

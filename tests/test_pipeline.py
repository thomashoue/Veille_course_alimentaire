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


class TestCollecteExplicite:
    """Fournir des relevés ne doit pas déclencher une collecte réseau."""

    def test_manual_seul_ne_collecte_pas(self, config, tmp_path, monkeypatch):
        import src.pipeline as pipeline

        appels = []
        monkeypatch.setattr(
            pipeline, "collect", lambda *a, **k: appels.append(1) or []
        )
        path = tmp_path / "manual.json"
        path.write_text(
            '[{"store_id": "leclerc_pleumeleuc", "basket_item_id": "lait_demi_ecreme",'
            ' "product_label": "Lait demi-écrémé 6x1L", "price_eur": 4.80,'
            ' "verified_in_drive": true}]',
            encoding="utf-8",
        )
        pipeline.run(config, manual_file=path, use_drive=False,
                     ledger_path=tmp_path / "o.sqlite")
        assert appels == []

    def test_collecte_forcee_si_demandee(self, config, tmp_path, monkeypatch):
        import src.pipeline as pipeline

        appels = []
        monkeypatch.setattr(
            pipeline, "collect", lambda *a, **k: appels.append(1) or []
        )
        path = tmp_path / "manual.json"
        path.write_text('[]', encoding="utf-8")
        pipeline.run(config, manual_file=path, collect_sources=True, use_drive=False,
                     ledger_path=tmp_path / "o.sqlite")
        assert appels == [1]


class TestParseDossier:
    """parse-page --dir : toute la moisson du vendredi en une commande."""

    def test_dossier_de_pages(self, config, tmp_path, capsys):
        import shutil
        from pathlib import Path

        from src.cli import main

        fixtures = Path(__file__).parent / "fixtures"
        pages = tmp_path / "pages"
        pages.mkdir()
        shutil.copy(fixtures / "leclerc_drive_tile.html", pages / "lait.html")
        shutil.copy(fixtures / "drive_search_saved.html", pages / "recherche2.html")
        out = tmp_path / "manual.json"

        code = main([
            "parse-page", "--store", "leclerc_pleumeleuc",
            "--dir", str(pages), "--out", str(out),
        ])
        assert code == 0
        import json as json_module

        rows = json_module.loads(out.read_text(encoding="utf-8"))
        assert len(rows) >= 4
        assert all(r["verified_in_drive"] for r in rows)

    def test_dossier_vide(self, config, tmp_path):
        from src.cli import main

        vide = tmp_path / "vide"
        vide.mkdir()
        assert main(["parse-page", "--store", "leclerc_pleumeleuc", "--dir", str(vide)]) == 1

    def test_ni_file_ni_dir(self, config):
        from src.cli import main

        assert main(["parse-page", "--store", "leclerc_pleumeleuc"]) == 2


class TestPaste:
    """Import direct de la réponse JSON de Claude dans Chrome (pas de Ctrl+S)."""

    def _paste(self, tmp_path, text, replace=False):
        from src.cli import main

        source = tmp_path / "colle.txt"
        source.write_text(text, encoding="utf-8")
        out = tmp_path / "manual.json"
        argv = ["paste", "--file", str(source), "--out", str(out)]
        if replace:
            argv.append("--replace")
        return main(argv), out

    def test_json_entoure_de_prose(self, config, tmp_path):
        import json as json_module

        code, out = self._paste(
            tmp_path,
            'Voici les relevés demandés :\n```json\n'
            '[{"store_id": "intermarche_montauban", "basket_item_id": "oeufs",'
            ' "product_label": "Œufs frais De Nos Régions x12", "price_eur": 2.77,'
            ' "verified_in_drive": true, "source": "drive"}]\n```\nDites-moi si besoin.',
        )
        assert code == 0
        rows = json_module.loads(out.read_text(encoding="utf-8"))
        assert rows[0]["price_eur"] == 2.77

    def test_magasin_inconnu_ecarte_avec_explication(self, config, tmp_path, capsys):
        code, _ = self._paste(
            tmp_path,
            '[{"store_id": "carrefour_city", "basket_item_id": "oeufs",'
            ' "product_label": "Œufs x12", "price_eur": 2.5}]',
        )
        assert code == 1
        sortie = capsys.readouterr().out
        assert "magasin inconnu" in sortie
        assert "intermarche_montauban" in sortie      # la liste valide est donnée

    def test_ajout_sans_ecraser(self, config, tmp_path):
        import json as json_module

        row = ('[{"store_id": "intermarche_montauban", "basket_item_id": "oeufs",'
               ' "product_label": "Œufs x12", "price_eur": 2.77}]')
        self._paste(tmp_path, row)
        code, out = self._paste(tmp_path, row)
        assert code == 0
        assert len(json_module.loads(out.read_text(encoding="utf-8"))) == 2

    def test_texte_sans_json(self, config, tmp_path):
        code, _ = self._paste(tmp_path, "désolé, je n'ai rien trouvé")
        assert code == 2


class TestOpenTabs:
    """Génération des onglets de recherche — sans réseau, sans cookie."""

    def test_script_windows(self, config, tmp_path, monkeypatch):
        import sys as sysmod

        from src.cli import main

        monkeypatch.setattr(sysmod, "platform", "win32")
        script = tmp_path / "ouvrir.bat"
        code = main(["open-tabs", "--store", "leclerc_pleumeleuc",
                     "--bulk", "--script", str(script)])
        assert code == 0
        contenu = script.read_text(encoding="utf-8")
        assert "start" in contenu
        assert "leclercdrive.fr" in contenu
        # --bulk ne prend que les postes à stocker, pas tout le panier.
        assert contenu.count("start") == sum(1 for i in config.items.values() if i.bulk_worthy)

    def test_script_shell(self, config, tmp_path, monkeypatch):
        import sys as sysmod

        from src.cli import main

        monkeypatch.setattr(sysmod, "platform", "linux")
        script = tmp_path / "ouvrir.sh"
        assert main(["open-tabs", "--store", "hyperu_yffiniac",
                     "--items", "lait_demi_ecreme", "--script", str(script)]) == 0
        assert "xdg-open" in script.read_text(encoding="utf-8")

    def test_magasin_sans_url(self, config, tmp_path, monkeypatch):
        import sys as sysmod

        from src.cli import main

        monkeypatch.setattr(sysmod, "platform", "linux")
        # Action n'a pas d'URL de recherche drive.
        code = main(["open-tabs", "--store", "action_pace", "--script", str(tmp_path / "x.sh")])
        assert code == 1


class TestReview:
    """La revue ne remonte que les doutes, avec de quoi les lever sur la fiche."""

    def _run(self, tmp_path, rows, prompt=False):
        import json as json_module

        from src.cli import main

        path = tmp_path / "m.json"
        path.write_text(json_module.dumps(rows), encoding="utf-8")
        argv = ["review", "--manual", str(path)]
        if prompt:
            argv.append("--prompt")
        return main(argv)

    def test_signale_base_de_poids_et_format(self, config, tmp_path, capsys):
        code = self._run(tmp_path, [
            {"store_id": "leclerc_pleumeleuc", "basket_item_id": "conserve_poisson",
             "product_label": "Sardines 135g", "price_eur": 1.89, "verified_in_drive": True},
            {"store_id": "leclerc_pleumeleuc", "basket_item_id": "steak_hache",
             "product_label": "Steak haché promo", "price_eur": 4.99, "verified_in_drive": True},
        ])
        out = capsys.readouterr().out
        assert code == 0
        assert "P5" in out and "P4" in out

    def test_rien_a_verifier_quand_tout_est_net(self, config, tmp_path, capsys):
        self._run(tmp_path, [
            {"store_id": "leclerc_pleumeleuc", "basket_item_id": "lait_demi_ecreme",
             "product_label": "Lait demi-écrémé 6x1L", "price_eur": 5.28, "verified_in_drive": True},
        ])
        assert "Aucun doute" in capsys.readouterr().out

    def test_prompt_extension_liste_les_url(self, config, tmp_path, capsys):
        self._run(tmp_path, [
            {"store_id": "leclerc_pleumeleuc", "basket_item_id": "steak_hache",
             "product_label": "Steak promo", "price_eur": 4.99, "verified_in_drive": True,
             "source_url": "https://x/produit/steak"},
        ], prompt=True)
        out = capsys.readouterr().out
        assert "Claude dans Chrome" in out
        assert "https://x/produit/steak" in out


class TestOpenTabsFenetre:
    def test_new_window_est_le_defaut(self, config, tmp_path, monkeypatch, capsys):
        import sys as sysmod

        import src.cli as cli

        # Pas de navigateur trouvé : on n'ouvre rien, mais le message doit
        # annoncer une NOUVELLE fenêtre, pas des onglets.
        monkeypatch.setattr(cli, "_find_chromium", lambda: None)
        monkeypatch.setattr(sysmod, "platform", "linux")
        opened = []
        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda url, new=0, autoraise=True: opened.append((url, new)))
        cli.main(["open-tabs", "--store", "leclerc_pleumeleuc", "--items", "lait_demi_ecreme"])
        out = capsys.readouterr().out
        assert "ouvelle fenêtre" in out
        assert opened and opened[0][1] == 1        # premier lien : new window

    def test_same_window_ajoute_aux_onglets(self, config, tmp_path, monkeypatch, capsys):
        import src.cli as cli

        opened = []
        import webbrowser

        monkeypatch.setattr(webbrowser, "open_new_tab", lambda url: opened.append(url))
        cli.main(["open-tabs", "--store", "leclerc_pleumeleuc",
                  "--items", "lait_demi_ecreme", "--same-window"])
        assert "onglets courants" in capsys.readouterr().out
        assert opened

"""Sortie : une liste PAR MAGASIN, plus un bloc WhatsApp (§8)."""

from src.assign import assign
from src.models import OK, Grade, Offer, PriceObservation
from src.normalize import normalize
from src.report import build_report


def offer(config, store_id, item_id, label, price, saving=2.0, grade=Grade.GOOD):
    observation = PriceObservation(
        store_id=store_id,
        basket_item_id=item_id,
        product_label=label,
        price_eur=price,
        verified_in_drive=True,
    )
    normalize(observation, config)
    return Offer(
        observation=observation,
        item=config.item(item_id),
        verdict=OK(),
        grade=grade,
        saving_eur=saving,
    )


class TestRapport:
    def test_groupe_par_magasin_et_par_personne(self, config):
        offers = [
            offer(config, "leclerc_pleumeleuc", "lait_demi_ecreme", "Lait 6x1L", 4.80),
            offer(config, "hyperu_yffiniac", "cafe", "Café moulu 1 kg", 8.00, saving=4.0),
        ]
        report = build_report(assign(offers, config), config)
        assert "E.Leclerc Pleumeleuc" in report.markdown
        assert "Charlotte" in report.markdown
        assert "Thomas" in report.markdown
        assert "Lait 6x1L" in report.markdown

    def test_bloc_whatsapp_present_et_court(self, config):
        offers = [offer(config, "leclerc_pleumeleuc", "lait_demi_ecreme", "Lait 6x1L", 4.80)]
        report = build_report(assign(offers, config), config)
        assert "🛒 Courses" in report.whatsapp
        assert report.whatsapp in report.markdown          # en tête, copier-coller
        assert len(report.whatsapp.splitlines()) < 30

    def test_conclure_a_l_absence_d_offre(self, config):
        # §10 : savoir dire « rien de conforme » plutôt que proposer un pis-aller.
        report = build_report(assign([], config), config)
        assert "Aucune offre conforme" in report.markdown
        assert "Litière chat" in report.markdown
        assert "prix courant" in report.markdown           # le conseil de repli

    def test_prix_normalise_affiche(self, config):
        offers = [offer(config, "leclerc_pleumeleuc", "lait_demi_ecreme", "Lait 6x1L", 4.80)]
        report = build_report(assign(offers, config), config)
        assert "0,800 €/L" in report.markdown   # 3 décimales sous 1 €

    def test_pistes_en_annexe_et_jamais_dans_les_listes(self, config):
        piste_obs = PriceObservation(
            store_id="leclerc_pleumeleuc",
            basket_item_id="croquettes_chat",
            product_label="Friskies 2 kg",
            price_eur=4.22,
            verified_in_drive=False,
        )
        normalize(piste_obs, config)
        piste = Offer(
            observation=piste_obs,
            item=config.item("croquettes_chat"),
            verdict=OK(),
            grade=Grade.GOOD,
        )
        report = build_report(assign([], config), config, pistes=[piste])
        assert "À vérifier avant achat" in report.markdown
        assert "Friskies" in report.markdown
        assert "Friskies" not in report.whatsapp

    def test_aucune_enseigne_exclue_dans_la_sortie(self, config):
        offers = [
            offer(config, "leclerc_pleumeleuc", "lait_demi_ecreme", "Lait 6x1L", 4.80),
            offer(config, "superu_breteil", "cafe", "Café moulu 1 kg", 8.00),
        ]
        report = build_report(assign(offers, config), config)
        texte = report.markdown.lower()
        assert "carrefour" not in texte
        assert "auchan" not in texte

    def test_magasins_ecartes_expliques(self, config):
        offers = [offer(config, "action_pace", "papier_toilette", "PQ 24 rouleaux", 4.97, saving=0.1)]
        report = build_report(assign(offers, config), config)
        assert "détour non amorti" in report.markdown

    def test_fichier_eml(self, config, tmp_path):
        report = build_report(assign([], config), config)
        eml = report.to_eml(["houe.thomas@gmail.com", "charlotte.barbe.ergo@gmail.com"])
        assert "houe.thomas@gmail.com" in eml
        assert "charlotte.barbe.ergo@gmail.com" in eml
        paths = report.write(tmp_path)
        assert paths["markdown"].exists()
        assert paths["whatsapp"].exists()


class TestLisibilite:
    def test_section_absence_priorise_les_postes_qui_comptent(self, config):
        from src.assign import assign
        from src.report import build_report

        report = build_report(assign([], config), config, observed_item_ids={"steak_hache"})
        markdown = report.markdown
        # Contrainte dure, poste à stocker, article effectivement relevé :
        # chacun mérite une phrase.
        assert "- **Litière chat** : rien de conforme" in markdown
        assert "- **Papier toilette** : rien de conforme" in markdown
        assert "- **Steak haché** : rien de conforme" in markdown
        # Le reste du panier tient en une ligne.
        assert "Rien de neuf sur :" in markdown
        assert "- **Mozzarella** : rien de conforme" not in markdown


class TestOffreReportee:
    def test_section_conforme_mais_pas_cette_semaine(self, config):
        from src.assign import assign
        from src.report import build_report

        offers = [offer(config, "superu_breteil", "litiere_chat",
                        "Litière charbon actif 5 L", 4.59, saving=0.15)]
        report = build_report(assign(offers, config), config)
        assert "Conforme, mais pas cette semaine" in report.markdown
        assert "Litière charbon actif 5 L" in report.markdown
        # Et surtout : elle ne doit PAS être annoncée comme introuvable.
        assert "- **Litière chat** : rien de conforme" not in report.markdown

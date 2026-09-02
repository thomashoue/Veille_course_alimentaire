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


class TestDiagnostic:
    """Découverte d'un gabarit inconnu — le cas Leclerc du 2026-08-31."""

    def test_repere_la_vignette_produit(self, page_html):
        from src.drive.offline import analyze_page

        analysis = analyze_page(page_html)
        top = analysis["candidates"][0]
        assert (top["tag"], top["class"]) == ("li", "produit")
        assert top["count"] == 4

    def test_page_sans_resultat(self):
        from src.drive.offline import analyze_page

        analysis = analyze_page("<html><body><p>Aucun résultat</p></body></html>")
        assert analysis["candidates"] == []

    def test_les_extraits_sont_masques(self):
        from src.drive.offline import analyze_page

        html = (
            '<div class="compte">Thomas — houe.thomas@gmail.com — carte 1234567890 '
            "— 4,80 €</div>"
        )
        sample = analyze_page(html)["candidates"][0]["sample"]
        assert "houe.thomas@gmail.com" not in sample
        assert "1234567890" not in sample
        assert "4,80 €" in sample          # le prix, lui, doit rester lisible


class TestGabaritLeclerc:
    """Gabarit réel de Leclerc Drive, relevé le 2026-08-31 sur une page de 822 Ko.

    Trois pièges y coexistent : le prix découpé en deux éléments, une vignette
    non fermée, et des produits sponsorisés qui ne sont pas l'assortiment.
    """

    @pytest.fixture
    def leclerc_html(self):
        return (FIXTURES / "leclerc_drive_tile.html").read_text(encoding="utf-8")

    def test_le_prix_decoupe_est_lu_en_entier(self, leclerc_html):
        products, _ = extract_products(leclerc_html)
        lait = next(p for p in products if "Eco+" in p.label)
        # 5 € et ,52 sont deux éléments distincts : lire 5,00 € coûterait
        # 52 centimes à chaque relevé.
        assert lait.price_eur == pytest.approx(5.52)

    def test_le_libelle_est_debarrasse_du_bruit(self, leclerc_html):
        products, _ = extract_products(leclerc_html)
        lait = next(p for p in products if "Eco+" in p.label)
        assert lait.label == "Lait demi-écrémé Eco+"

    def test_vignette_non_fermee_quand_meme_lue(self, leclerc_html):
        # La deuxième vignette n'a pas de </div> : elle doit ressortir malgré tout.
        products, _ = extract_products(leclerc_html)
        assert any("Croquettes" in p.label for p in products)

    def test_les_sponsorises_sont_ecartes(self, leclerc_html):
        products, _ = extract_products(leclerc_html)
        assert not any("Lactel" in p.label for p in products)

    def test_le_format_est_deduit_du_prix_au_litre(self, leclerc_html, config):
        store = config.store("leclerc_pleumeleuc")
        observations, report = observations_from_page(leclerc_html, store, config)
        lait = next(o for o in observations if "Eco+" in o.product_label)
        # Le libellé ne dit pas « 6x1L » ; 5,52 ÷ 0,92 le retrouve.
        assert lait.pack_size == pytest.approx(6.0)
        assert lait.pack_unit == "l"
        assert report["packs_derived_from_unit_price"] >= 1
        assert "déduit du prix unitaire" in " ".join(lait.notes)

    def test_le_prix_normalise_tombe_juste(self, leclerc_html, config):
        from src.normalize import normalize

        store = config.store("leclerc_pleumeleuc")
        observations, _ = observations_from_page(leclerc_html, store, config)
        lait = next(o for o in observations if "Eco+" in o.product_label)
        normalize(lait, config)
        assert lait.unit_price == pytest.approx(0.92)

    def test_le_diagnostic_designe_la_bonne_classe(self, leclerc_html):
        from src.drive.offline import analyze_page

        candidates = analyze_page(leclerc_html)["candidates"]
        # La classe est rendue telle quelle, pour être recopiée dans un sélecteur.
        assert candidates[0]["class"] == "liWCRS310_Product"
        assert candidates[0]["tag"] == "li"


class TestPiegesLitiere:
    """Les deux bugs du rapport du 2026-08-31, rejoués tels quels.

    Une litière pour rongeurs était devenue « record » dans le panier de
    Charlotte, avec pour prix le prix au litre pris pour le prix du pack.
    """

    @pytest.fixture
    def piege_html(self):
        return (FIXTURES / "leclerc_litiere_piege.html").read_text(encoding="utf-8")

    def test_la_litiere_rongeurs_n_est_plus_une_litiere_chat(self, piege_html, config):
        store = config.store("leclerc_pleumeleuc")
        observations, _ = observations_from_page(piege_html, store, config)
        assert not any("rongeurs" in o.product_label for o in observations)

    def test_le_prix_au_litre_est_reconstruit_en_prix_de_pack(self, piege_html, config):
        from src.normalize import normalize

        store = config.store("leclerc_pleumeleuc")
        observations, _ = observations_from_page(piege_html, store, config)
        agglo = next(o for o in observations if "agglomérante" in o.product_label)
        # Vignette sans prix de boîte, seulement « 1,32 € / l » : on reconstruit
        # le prix du pack (1,32 €/L x 15 L = 19,80 €) au lieu de le prendre pour
        # le prix de la boîte. Plus de quarantaine — le bug racine est corrigé.
        assert agglo.price_eur == pytest.approx(19.80)
        normalize(agglo, config)
        assert agglo.unit_price == pytest.approx(1.32)

    def test_un_prix_coherent_ne_devient_pas_suspect(self, piege_html, config):
        store = config.store("leclerc_pleumeleuc")
        observations, _ = observations_from_page(piege_html, store, config)
        confort = next(o for o in observations if "confort" in o.product_label)
        assert confort.suspect_reason is None

    def test_rien_de_douteux_dans_la_liste_de_courses(self, piege_html, config, tmp_path):
        """Bout en bout : le rapport final ne doit contenir aucun des deux pièges."""
        from src.pipeline import run

        store = config.store("leclerc_pleumeleuc")
        observations, _ = observations_from_page(piege_html, store, config)
        result = run(config, observations=observations, use_drive=False,
                     ledger_path=tmp_path / "o.sqlite")

        dans_les_paniers = [
            offer.observation.product_label
            for basket in result.plan.baskets
            for offer in basket.offers
        ]
        # Le prix suspect ne fait pas une offre…
        assert not any("agglomérante 15L" in label for label in dans_les_paniers)
        # …mais il n'est pas perdu : il sort en « à vérifier », motivé.
        assert "À vérifier avant achat" in result.report.markdown
        assert "prix incohérent" in result.report.markdown
        # La litière « confort », au type indéterminable depuis le libellé,
        # ne passe pas non plus : contrainte dure invérifiable ≠ conforme.
        assert not any("confort" in label for label in dans_les_paniers)
        assert "invérifiable" in result.report.markdown


class TestGabaritCoursesU:
    """Gabarit réel de Courses U (SFCC), relevé le 2026-08-31 à Yffiniac.

    Spécificités : le libellé apparaît deux fois (texte caché + lien), et sur
    une promo le prix BARRÉ (price-standard) est rendu avant le prix réel
    (price-sales) — le « premier prix du texte » serait le mauvais.
    """

    @pytest.fixture
    def u_html(self):
        return (FIXTURES / "coursesu_tiles.html").read_text(encoding="utf-8")

    def test_les_vignettes_sont_lues(self, u_html):
        products, method = extract_products(u_html)
        assert method == "blocs HTML"
        assert len(products) == 2

    def test_le_libelle_duplique_est_replie(self, u_html):
        products, _ = extract_products(u_html)
        tranquille = next(p for p in products if "TRANQUILLE" in p.label)
        assert tranquille.label == "Litière bi carbonite, TRANQUILLE, sac 5L"

    def test_le_prix_barre_n_est_pas_pris_pour_le_prix(self, u_html):
        products, _ = extract_products(u_html)
        promo = next(p for p in products if "charbon" in p.label)
        assert promo.price_eur == pytest.approx(4.75)     # price-sales
        assert promo.regular_price == pytest.approx(9.50)  # price-standard

    def test_le_prix_unitaire_affiche_est_capture(self, u_html):
        products, _ = extract_products(u_html)
        promo = next(p for p in products if "charbon" in p.label)
        assert (promo.unit_price_hint, promo.unit_hint_unit) == (0.48, "l")

    def test_une_vraie_promo_de_drive_a_moitie_prix_n_est_pas_rejetee(self, u_html, config):
        """P1 vise les agrégateurs : sur une page de drive, un prix barré au
        double exact est une vraie promo à −50 %, pas une donnée trafiquée."""
        from src.pipeline import run

        store = config.store("hyperu_yffiniac")
        observations, _ = observations_from_page(u_html, store, config)
        result = run(config, observations=observations, use_drive=False)
        offres = [o.observation.product_label for o in result.offers]
        assert any("charbon" in label for label in offres)

    def test_bout_en_bout_yffiniac_chez_thomas(self, u_html, config, tmp_path):
        from src.pipeline import run

        store = config.store("hyperu_yffiniac")
        observations, _ = observations_from_page(u_html, store, config)
        result = run(config, observations=observations, use_drive=False,
                     ledger_path=tmp_path / "o.sqlite")
        # 0,475 €/L en charbon actif : sous le seuil 0,92, affaire réelle.
        charbon = next(o for o in result.offers if "charbon" in o.observation.product_label)
        assert charbon.observation.attributes["type"] == "agglo_charbon"
        assert charbon.unit_price == pytest.approx(0.475)
        assert result.plan.baskets[0].store.id == "hyperu_yffiniac"
        assert result.plan.baskets[0].assignee == "thomas"
        # La « bi carbonite » au type inconnu reste hors liste, en à-vérifier.
        assert not any("TRANQUILLE" in o.observation.product_label for o in result.offers)


class TestGardeFousIntermarche:
    """Deux pièges propres à Intermarché, vécus pendant l'expérimentation."""

    def test_le_net_egoutte_du_libelle_est_lu(self, config):
        html = (
            '<ul><li class="liWCRS310_Product"><div class="divWCRS310_Content">'
            "<a>Sardines à l'huile poids net égoutté 93 g</a>"
            '<button>Ajouter au panier</button>'
            '<span class="spanWCRS310_Prix">2 €</span><span>,09</span>'
            "</div></li></ul>"
        )
        store = config.store("intermarche_montauban")
        observations, _ = observations_from_page(html, store, config)
        assert observations[0].weight_basis == "net_egoutte"

    def test_ville_absente_de_la_page_signalee(self, config):
        # Se connecter à un autre magasin bascule tout le compte : si la page
        # ne mentionne jamais la ville attendue, on doit le dire.
        html = '<div class="produit">Magasin de Lamballe — Lait 6x1L 5,28 €</div>'
        store = config.store("intermarche_montauban")
        _, report = observations_from_page(html, store, config)
        assert report["store_city_seen"] is False

    def test_ville_presente_ok(self, config):
        html = '<div>Intermarché Montauban-de-Bretagne <span class="produit">Lait 6x1L 5,28 €</span></div>'
        store = config.store("intermarche_montauban")
        _, report = observations_from_page(html, store, config)
        assert report["store_city_seen"] is True


class TestFauxPositifsIntermarche:
    """Pièges de rattachement relevés sur une vraie page Intermarché (174 produits).

    Le rattachement par sous-chaîne prenait « chorizo » pour du riz, le fromage
    « Soignon » pour des oignons, un yaourt banane pour des bananes. Les plats
    préparés et desserts empruntaient le nom d'un ingrédient du panier.
    """

    @pytest.mark.parametrize("label", [
        "Charal Burger spicy chorizo la boîte de 4 620g",
        "Soignon La Bûche Extra fondante la bûche de 180 g",
        "Yoplait Yop Yaourt à boire aromatisé fraise banane",
        "La Laitière Crème aux œufs sur lit de caramel les 4 pots",
        "Savernou Saucisson sec comté noisette 100g",
        "Sodebo Wraper's wrap poulet pané cheddar oignon frits",
        "Monique Ranou Maxi bacon burger 195g",
        "Mamie Nova Gourmand Dessert Cœur de Liégeois chocolat",
    ])
    def test_les_faux_positifs_sont_ecartes(self, config, label):
        assert config.match_item(label) is None

    @pytest.mark.parametrize("label, expected", [
        ("Pâturages Intermarché Lait demi-écrémé les 6x1L", "lait_demi_ecreme"),
        ("Panzani Pâtes Torti Les 3 minutes 1kg", "pates"),
        ("Odyssée Intermarché thon piquant 120 g", "conserve_poisson"),
        ("Président Emmental râpé fondant sachet 350 g", "emmental_rape"),
        ("Café moulu Classique pur arabica 500 g", "cafe"),
    ])
    def test_les_vrais_produits_matchent_toujours(self, config, label, expected):
        assert config.match_item(label).id == expected

    def test_fruits_legumes_pas_cherches_en_drive(self, config):
        # Hors périmètre drive : « Oignons rouges 1kg » ne remonte pas d'une
        # page de drive, même si le mot correspond.
        assert config.match_item("Oignons rouges 1kg") is None
        assert config.match_item("Oignons rouges 1kg", include_out_of_scope=True) is not None


class TestGabaritIntermarche:
    """Structure réelle des cartes Intermarché (relevé 2026-08-31, Montauban).

    La carte affiche le prix de la boîte (2,44 €) ET le prix au kilo net
    égoutté (28,05 €/Kg) dans des éléments distincts. Le lecteur prend la boîte,
    lit le €/kg comme repère, et détecte le net égoutté (piège brut/égoutté).
    """

    @pytest.fixture
    def inter_html(self):
        return (FIXTURES / "intermarche_cards.html").read_text(encoding="utf-8")

    def test_le_prix_de_la_boite_pas_le_prix_au_kilo(self, inter_html, config):
        store = config.store("intermarche_montauban")
        observations, _ = observations_from_page(inter_html, store, config)
        odyssee = next(o for o in observations if "Odyssée" in o.product_label)
        assert odyssee.price_eur == pytest.approx(2.44)     # la boîte, pas 28,05

    def test_le_net_egoutte_est_detecte(self, inter_html, config):
        store = config.store("intermarche_montauban")
        observations, _ = observations_from_page(inter_html, store, config)
        assert all(
            o.weight_basis == "net_egoutte"
            for o in observations if "sardine" in o.product_label.lower()
        )

    def test_la_suggestion_recette_par_personne_est_ignoree(self, inter_html, config):
        store = config.store("intermarche_montauban")
        observations, _ = observations_from_page(inter_html, store, config)
        assert not any("Sandwich" in o.product_label for o in observations)

    def test_le_magasin_actif_est_reconnu_malgre_la_ponctuation(self, inter_html, config):
        # « Montauban-de-Bretagne » (config) == « Montauban De Bretagne » (page).
        store = config.store("intermarche_montauban")
        _, report = observations_from_page(inter_html, store, config)
        assert report["store_city_seen"] is True

    def test_comparaison_sardines_sur_net_egoutte(self, inter_html, config):
        from src.normalize import normalize

        store = config.store("intermarche_montauban")
        observations, _ = observations_from_page(inter_html, store, config)
        top = next(o for o in observations if "Top Budget" in o.product_label)
        normalize(top, config)
        assert top.unit_price == pytest.approx(8.6, abs=0.1)   # €/kg net, comparable


class TestDetectStore:
    """Auto-détection du magasin d'une page (dossier mêlé SingleFile)."""

    def test_par_url_de_drive(self, config):
        from src.drive.offline import detect_store

        html = "<!-- https://fd7-courses.leclercdrive.fr/magasin-173501-173501-Pleumeleuc/x --><body>WCRS310</body>"
        assert detect_store(html, config) == "leclerc_pleumeleuc"

    def test_url_la_plus_specifique_gagne(self, config):
        from src.drive.offline import detect_store

        # coursesu.com générique ET /drive-hyperu-yffiniac : le plus précis gagne.
        html = "<body>https://www.coursesu.com/drive-hyperu-yffiniac product-tile</body>"
        assert detect_store(html, config) == "hyperu_yffiniac"

    def test_par_gabarit_sans_url(self, config):
        from src.drive.offline import detect_store

        assert detect_store("<div class='stime-product-card-course'>x</div>", config) == "intermarche_montauban"

    def test_page_non_reconnue(self, config):
        from src.drive.offline import detect_store

        assert detect_store("<html><body>rien</body></html>", config) is None

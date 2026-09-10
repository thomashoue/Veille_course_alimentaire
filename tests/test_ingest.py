"""Collecte : extraction HTML et respect des sources (§6).

Aucun test ne fait de requête : les gabarits sont figés dans tests/fixtures.
"""

from pathlib import Path

import pytest

from src.ingest.base import Collector, slugify
from src.ingest.html_extract import (
    detect_loyalty_pct,
    detect_mechanic,
    detect_weight_basis,
    extract_jsonld,
    find_blocks,
    parse_all_prices,
    parse_price,
)
from src.ingest.http import Fetcher, SourceBlocked
from src.ingest.promocatalogues import PromocataloguesCollector

FIXTURES = Path(__file__).parent / "fixtures"


class TestExtraction:
    @pytest.mark.parametrize(
        "texte, attendu",
        [
            ("Prince 1,2 kg à 2,51 €", 2.51),
            ("Friskies 2 kg — € 4.22", 4.22),
            ("Bigard 640 g 9 € 99", 9.99),
            ("Ultima 9 kg 26,95 €", 26.95),
            ("pas de prix ici", None),
        ],
    )
    def test_lecture_des_prix(self, texte, attendu):
        assert parse_price(texte) == attendu

    def test_prix_habituel_et_promo(self):
        regular, promo = Collector.regular_and_promo("2,51 € au lieu de 5,02 €")
        assert (regular, promo) == (5.02, 2.51)

    @pytest.mark.parametrize(
        "texte, attendu",
        [
            ("-30% sur le 2ème article", "second_-30"),
            ("Le 2e à -50%", "second_-50"),
            ("2ème à moitié prix", "second_-50"),
            ("3 pour 2", "3_pour_2"),
            ("Lot de 4 boîtes", "lot"),
            ("Prix choc", None),
        ],
    )
    def test_detection_des_mecaniques(self, texte, attendu):
        assert detect_mechanic(texte) == attendu

    def test_base_de_poids(self):
        assert detect_weight_basis("poids net égoutté 93 g") == "net_egoutte"
        assert detect_weight_basis("poids net 140 g") == "brut"
        # Sans mention : on ne devine pas, ce sera un FLAG P5.
        assert detect_weight_basis("boîte de 140 g") is None

    def test_avantage_carte(self):
        assert detect_loyalty_pct("20% sur votre carte fidélité") == 20.0

    def test_blocs_et_jsonld(self):
        html = (FIXTURES / "promocatalogues_croquettes.html").read_text(encoding="utf-8")
        assert len(find_blocks(html, "div", "offer")) == 3
        assert any(n.get("@type") == "Product" for n in extract_jsonld(html))
        # Le JSON-LD ne doit pas polluer la lecture de texte des blocs.
        assert parse_all_prices(find_blocks(html, "div", "offer")[0].text) == [26.95, 53.90]


class TestPromocatalogues:
    def test_slash_final_obligatoire(self, config):
        collector = PromocataloguesCollector(config, Fetcher(config, offline=True))
        url = collector.offer_url("croquettes chat")
        assert url.endswith("/")
        assert "offres/croquettes-chat/" in url

    def test_parse_une_page(self, config):
        collector = PromocataloguesCollector(config, Fetcher(config, offline=True))
        html = (FIXTURES / "promocatalogues_croquettes.html").read_text(encoding="utf-8")
        observations = collector.parse(html, "croquettes_chat", "http://x/")

        labels = [o.product_label for o in observations]
        assert any("Friskies" in label for label in labels)
        # Aucune enseigne exclue ne doit ressortir de la collecte.
        assert all("carrefour" not in (o.banner or "") for o in observations)
        # Invariant C1 : la collecte ne vérifie rien.
        assert all(o.verified_in_drive is False for o in observations)

    def test_mecanique_et_prix_habituel_captures(self, config):
        collector = PromocataloguesCollector(config, Fetcher(config, offline=True))
        html = (FIXTURES / "promocatalogues_croquettes.html").read_text(encoding="utf-8")
        cards = collector._from_cards(html, "croquettes_chat", "http://x/")
        ultima = next(o for o in cards if "Ultima" in o.product_label)
        assert ultima.price_eur == 26.95
        assert ultima.regular_price == 53.90        # ratio 2,00 → P1 le rejettera
        u = next(o for o in cards if "Super U" in o.product_label)
        assert u.mechanic == "second_-30"

    def test_slug(self):
        assert slugify("Lait demi-écrémé") == "lait-demi-ecreme"


class TestSourcesInterdites:
    def test_deny_list_appliquee_avant_toute_requete(self, config):
        fetcher = Fetcher(config, offline=True)
        for url in (
            "https://www.e.leclerc/promo",          # 403 constaté
            "https://anti-crise.fr/x",
            "https://www.carrefour.fr/promo",
            "https://kimbino.fr/catalogues",
        ):
            with pytest.raises(SourceBlocked):
                fetcher.check_url(url)

    def test_hors_allow_list_refuse(self, config):
        fetcher = Fetcher(config, offline=True)
        with pytest.raises(SourceBlocked):
            fetcher.check_url("https://exemple-inconnu.fr/offres")

    def test_source_autorisee_passe(self, config):
        fetcher = Fetcher(config, offline=True)
        fetcher.respect_robots = False        # pas de réseau dans les tests
        fetcher.check_url("https://www.promocatalogues.fr/offres/lait/")


class TestCoupeCircuit:
    """Un hôte injoignable ne doit pas être interrogé 200 fois dans un run."""

    def test_hote_ecarte_apres_trois_echecs(self, config):
        fetcher = Fetcher(config, offline=True)
        fetcher.respect_robots = False
        url = "https://www.promocatalogues.fr/offres/lait/"
        fetcher._failures["www.promocatalogues.fr"] = 3
        with pytest.raises(SourceBlocked, match="échecs consécutifs"):
            fetcher.check_url(url)

    def test_les_autres_hotes_restent_ouverts(self, config):
        fetcher = Fetcher(config, offline=True)
        fetcher.respect_robots = False
        fetcher._failures["www.promocatalogues.fr"] = 3
        fetcher.check_url("https://www.vos-promos.fr/produits/lait")

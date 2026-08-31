"""Clients drive : rapprochement de libellés et idempotence du panier (§7)."""

import pytest

from src.drive.base import CartLine, DriveError, DriveProduct, FixtureDriveClient, best_match, match_score
from src.drive.leclerc import parse_search_xhr
from src.drive.verify import verify_in_drive, verify_observation
from src.models import PriceObservation
from src.normalize import normalize


@pytest.fixture
def store(config):
    return config.store("leclerc_pleumeleuc")


class TestRapprochement:
    def test_meme_produit_libelle_different(self):
        assert match_score("Litière chat silice cristaux 5 L", "Litière silice 5 L") > 0.4

    def test_format_different_n_est_pas_le_meme_produit(self):
        proche = match_score("Sardines à l'huile 140 g", "Sardines à l'huile 140 g")
        loin = match_score("Sardines à l'huile 3x93 g", "Sardines à l'huile 140 g")
        assert proche > loin

    def test_aucun_rapprochement_sous_le_seuil(self):
        produits = [DriveProduct(ref="1", label="Lessive liquide 45 lavages")]
        assert best_match(produits, "Croquettes chat 2 kg") is None


class TestVerificationDrive:
    """Constat C1 : le catalogue n'est pas l'assortiment du drive."""

    def test_produit_absent_reste_une_piste(self, config, store):
        client = FixtureDriveClient(store, {"lessive": [DriveProduct("1", "Lessive 45 lavages", 8.0)]})
        obs = PriceObservation(
            store_id=store.id,
            basket_item_id="croquettes_chat",
            product_label="Friskies 2 kg",
            price_eur=4.22,
        )
        verify_observation(obs, client)
        assert obs.verified_in_drive is False
        assert "absent du drive" in " ".join(obs.notes)

    def test_le_prix_du_drive_remplace_celui_du_prospectus(self, config, store):
        client = FixtureDriveClient(
            store, {"litiere": [DriveProduct("A1", "Litière chat silice cristaux 5 L", 6.20)]}
        )
        obs = PriceObservation(
            store_id=store.id,
            basket_item_id="litiere_chat",
            product_label="Litière silice 5 L",
            price_eur=4.99,          # prix prospectus = prix MAGASIN
        )
        verify_observation(obs, client)
        assert obs.verified_in_drive is True
        assert obs.price_eur == 6.20
        assert obs.source == "drive"
        assert "corrigé par le drive" in " ".join(obs.notes)

    def test_produit_indisponible_non_actionnable(self, config, store):
        client = FixtureDriveClient(
            store, {"cafe": [DriveProduct("C1", "Café moulu 1 kg", 9.0, available=False)]}
        )
        obs = PriceObservation(
            store_id=store.id,
            basket_item_id="cafe",
            product_label="Café moulu 1 kg",
            price_eur=9.0,
        )
        verify_observation(obs, client)
        assert not obs.is_actionable

    def test_statistiques_de_verification(self, config, store):
        client = FixtureDriveClient(store, {"lait": [DriveProduct("L1", "Lait demi-écrémé 6x1L", 5.10)]})
        trouve = PriceObservation(
            store_id=store.id, basket_item_id="lait_demi_ecreme",
            product_label="Lait demi-écrémé 6x1L", price_eur=5.10,
        )
        absent = PriceObservation(
            store_id=store.id, basket_item_id="croquettes_chat",
            product_label="Ultima 9 kg", price_eur=26.95,
        )
        _, stats = verify_in_drive([trouve, absent], config, {store.id: client})
        assert (stats.found, stats.absent) == (1, 1)
        assert stats.absence_rate == pytest.approx(0.5)

    def test_magasin_sans_client_est_ignore(self, config):
        obs = PriceObservation(
            store_id="action_tregueux", basket_item_id="papier_toilette",
            product_label="PQ 24 rouleaux", price_eur=4.97,
        )
        _, stats = verify_in_drive([obs], config, {})
        assert stats.checked == 0


class TestPanierIdempotent:
    """Les clics échouent silencieusement : on relit toujours le panier (§7)."""

    def test_ajout_verifie(self, store):
        client = FixtureDriveClient(store)
        produit = DriveProduct("P1", "Lait demi-écrémé 6x1L", 5.10)
        ligne = client.cart_add(produit, 2)
        assert ligne.quantity == 2
        assert len(client.cart_state()) == 1

    def test_deuxieme_appel_ne_double_pas_la_quantite(self, store):
        client = FixtureDriveClient(store)
        produit = DriveProduct("P1", "Lait demi-écrémé 6x1L", 5.10)
        client.cart_add(produit, 2)
        client.cart_add(produit, 2)
        assert client.cart_state()[0].quantity == 2

    def test_clic_silencieusement_perdu_est_rattrape(self, store):
        class ClicPerdu(FixtureDriveClient):
            """Reproduit le comportement constaté : le premier clic ne fait rien."""

            appels = 0

            def _add(self, product, quantity):
                ClicPerdu.appels += 1
                if ClicPerdu.appels == 1:
                    return
                super()._add(product, quantity)

        client = ClicPerdu(store)
        ligne = client.cart_add(DriveProduct("P1", "Lait 6x1L", 5.10), 1)
        assert ligne.quantity == 1
        assert ClicPerdu.appels == 2

    def test_echec_persistant_leve_une_erreur(self, store):
        class JamaisAjoute(FixtureDriveClient):
            def _add(self, product, quantity):
                return

        with pytest.raises(DriveError):
            JamaisAjoute(store).cart_add(DriveProduct("P1", "Lait 6x1L", 5.10), 1)

    def test_dry_run_ne_touche_rien(self, store):
        client = FixtureDriveClient(store, dry_run=True)
        client.cart_add(DriveProduct("P1", "Lait 6x1L", 5.10), 1)
        assert client.cart_state() == []


class TestXHRLeclerc:
    def test_lecture_json(self):
        produits = parse_search_xhr(
            '{"produits":[{"ref":"123","libelle":"Lait demi-écrémé 6x1L",'
            '"prix":"5,10","disponible":true}]}'
        )
        assert len(produits) == 1
        assert produits[0].price_eur == pytest.approx(5.10)
        assert produits[0].pack.total_base == pytest.approx(6.0)

    def test_json_inattendu_ne_casse_pas(self):
        assert parse_search_xhr('{"autre": 1}') == []

"""Veille des e-mails de commande (Intermarché drive)."""

from pathlib import Path

import pytest

from src.ingest_mail import detect_store, parse_order, read_email, to_observations
from src.normalize import normalize

FIXTURE = Path(__file__).parent / "fixtures" / "mail_intermarche_commande.txt"


@pytest.fixture
def order(config):
    return parse_order(read_email(FIXTURE), config)


class TestParseCommande:
    def test_enseigne_et_ville(self, config):
        # accent (« Intermarché ») et mot entier (« u » ne matche pas partout)
        assert detect_store(read_email(FIXTURE), config) == "intermarche_montauban"

    def test_entete(self, order):
        assert order.reference == "525147373"
        assert order.total_eur == pytest.approx(58.30)
        assert order.order_date.isoformat() == "2026-09-07"

    def test_les_26_lignes_rattachees(self, order):
        assert len(order.lines) == 26
        assert order.unmatched == []

    def test_prix_normalise_par_article(self, config, order):
        by_item = {}
        for obs in to_observations(order, config):
            normalize(obs, config)
            by_item.setdefault(obs.basket_item_id, []).append(obs)
        riz = by_item["riz"][0]
        assert riz.best_unit_price == pytest.approx(2.76, abs=0.01)   # 1,38 € / 0,5 kg
        oeufs = by_item["oeufs"][0]
        assert oeufs.best_unit_price == pytest.approx(0.2545, abs=0.001)  # 5,09 € / 20
        epin = by_item["epinards_surgeles"][0]
        assert epin.best_unit_price == pytest.approx(3.00, abs=0.01)   # 1,80 € / 0,6 kg

    def test_net_egoutte_marque(self, config, order):
        rouges = next(o for o in order.lines if o.basket_item_id == "legumineuses_conserve")
        assert rouges.weight_basis == "net_egoutte"

    def test_observations_verifiees_en_drive(self, config, order):
        obs = to_observations(order, config)
        assert obs and all(o.verified_in_drive and o.source == "drive" for o in obs)
        assert all(o.store_id == "intermarche_montauban" for o in obs)

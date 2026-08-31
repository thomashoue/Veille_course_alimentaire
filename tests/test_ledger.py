"""Historique et détection de « vrai » record (backlog P1)."""

import pytest

from src.models import PriceObservation
from src.normalize import normalize


def obs_at(config, price, store="superu_breteil", item="litiere_chat",
           label="Litière charbon actif 5 L", verified=True):
    observation = PriceObservation(
        store_id=store,
        basket_item_id=item,
        product_label=label,
        price_eur=price,
        verified_in_drive=verified,
    )
    normalize(observation, config)
    return observation


class TestRecord:
    def test_premier_releve_est_un_record(self, config, ledger):
        assert ledger.check_record(obs_at(config, 4.59), config).is_record

    def test_baisse_marginale_n_est_pas_un_record(self, config, ledger):
        ledger.record(obs_at(config, 4.59))
        # 0,9 % de mieux : du bruit, pas une trouvaille à annoncer.
        check = ledger.check_record(obs_at(config, 4.55), config)
        assert not check.is_record
        assert check.previous_best == pytest.approx(0.918)

    def test_vraie_baisse_est_un_record(self, config, ledger):
        ledger.record(obs_at(config, 4.59))
        assert ledger.check_record(obs_at(config, 3.99), config).is_record

    def test_les_pistes_ne_font_pas_record(self, config, ledger):
        piste = obs_at(config, 1.00, verified=False)
        assert not ledger.check_record(piste, config).is_record

    def test_record_par_type_de_litiere(self, config, ledger):
        # La silice et l'agglo charbon ne jouent pas dans la même catégorie :
        # un record silice ne doit pas être comparé à un prix charbon.
        ledger.record(obs_at(config, 4.59, label="Litière charbon actif 5 L"))
        silice = obs_at(config, 9.00, label="Litière cristaux de silice 5 L")
        assert ledger.check_record(silice, config).is_record


class TestHistorique:
    def test_stockage_des_pistes_aussi(self, config, ledger):
        # Les pistes sont stockées : elles orientent la vérification suivante.
        ledger.record(obs_at(config, 4.59, verified=False))
        assert len(ledger.history("litiere_chat")) == 1

    def test_meilleur_prix_ignore_les_pistes(self, config, ledger):
        ledger.record(obs_at(config, 1.00, verified=False))
        ledger.record(obs_at(config, 4.59, verified=True))
        best = ledger.best_price("litiere_chat")
        assert best["price_eur"] == pytest.approx(4.59)

    def test_idempotence(self, config, ledger):
        observation = obs_at(config, 4.59)
        ledger.record(observation)
        ledger.record(observation)
        assert len(ledger.history("litiere_chat")) == 1

    def test_run_enregistre(self, ledger):
        run_id = ledger.start_run()
        ledger.finish_run(run_id, 12, 3, "ok")
        row = ledger.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["n_offers"] == 3

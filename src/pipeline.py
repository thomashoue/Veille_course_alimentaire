"""Pipeline hebdomadaire — §8.

    collect (agrégateurs) → normalize → validate → shortlist
       → verify_in_drive (le seul filtre qui compte)
       → assign (corridor + personne)
       → report (par magasin) → e-mail + bloc WhatsApp
       → ledger.update (records, tendances)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .assign import Plan, assign
from .config import DATA_DIR, Config, get_config
from .drive import DriveClient, FixtureDriveClient, get_client, verify_in_drive
from .drive.base import DriveProduct
from .drive.verify import VerificationStats
from .ingest import COLLECTORS
from .ingest.http import Fetcher
from .ledger import Ledger
from .models import Grade, Offer, PriceObservation, Status, Verdict
from .normalize import normalize
from .report import Report, build_report
from .validate import grade, is_reportable, saving_vs_threshold, validate

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    plan: Plan
    report: Report
    offers: list[Offer]
    pistes: list[Offer]
    observations: list[PriceObservation]
    stats: VerificationStats = field(default_factory=VerificationStats)

    def counts(self) -> dict[str, int]:
        return {
            "observations": len(self.observations),
            "offres": len(self.offers),
            "pistes": len(self.pistes),
            "magasins": len(self.plan.baskets),
            "sans_offre": len(self.plan.unmatched),
        }


# --------------------------------------------------------------------------- #
# Étapes
# --------------------------------------------------------------------------- #
def collect(config: Config, item_ids: list[str] | None = None, offline: bool = False) -> list[PriceObservation]:
    """Interroge les agrégateurs actifs. Produit des PISTES, jamais des offres."""
    fetcher = Fetcher(config, offline=offline)
    observations: list[PriceObservation] = []
    for name, collector_class in COLLECTORS.items():
        collector = collector_class(config, fetcher)
        if not collector.enabled:
            log.info("collecteur %s désactivé", name)
            continue
        found = collector.collect(item_ids)
        log.info("collecteur %s : %s piste(s)", name, len(found))
        observations += found
    return observations


def load_observations(path: Path | str, config: Config) -> list[PriceObservation]:
    """Relevés saisis à la main (JSON) — la voie de secours quand un gabarit casse.

    Format attendu : une liste d'objets dont les clés sont celles de
    PriceObservation. Les dates sont en ISO.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    known = set(PriceObservation.__dataclass_fields__)
    observations = []
    for entry in raw:
        data = {k: v for k, v in entry.items() if k in known}
        for key in ("loyalty_valid_until", "valid_from", "valid_until"):
            if isinstance(data.get(key), str):
                data[key] = date.fromisoformat(data[key])
        if isinstance(data.get("observed_at"), str):
            data["observed_at"] = datetime.fromisoformat(data["observed_at"])
        data.pop("id", None)
        observations.append(PriceObservation(**data))
    return observations


def shortlist(
    observations: list[PriceObservation], config: Config, pickup_date: date | None = None
) -> tuple[list[PriceObservation], dict[str, Verdict]]:
    """Normalise, valide, et écarte ce qui est faux ou non conforme.

    C'est ici que les prix inventés par les agrégateurs (C2) et les produits
    hors contrainte (litière minérale) disparaissent, avant tout accès réseau
    au drive : on ne va pas vérifier en drive ce qu'on sait déjà faux.
    """
    kept: list[PriceObservation] = []
    verdicts: dict[str, Verdict] = {}
    for obs in observations:
        if obs.basket_item_id not in config.items:
            continue
        normalize(obs, config, pickup_date=pickup_date)
        verdict = validate(obs, config, pickup_date=pickup_date)
        verdicts[obs.id] = verdict
        if verdict.status is not Status.REJECT:
            kept.append(obs)
        else:
            log.debug("écarté : %s — %s", obs.product_label, verdict.explain())
    return kept, verdicts


def build_offers(
    observations: list[PriceObservation],
    config: Config,
    ledger: Ledger | None = None,
    pickup_date: date | None = None,
) -> tuple[list[Offer], list[Offer]]:
    """Transforme les observations validées en offres notées.

    Renvoie ``(offres actionnables, pistes)``. Une piste ne peut jamais entrer
    dans une liste de courses : c'est l'invariant central du §3.
    """
    offers: list[Offer] = []
    pistes: list[Offer] = []
    for obs in observations:
        item = config.items[obs.basket_item_id]
        normalize(obs, config, item, pickup_date=pickup_date)
        verdict = validate(obs, config, item, pickup_date=pickup_date)
        note = grade(obs, config, item)
        offer = Offer(
            observation=obs,
            item=item,
            verdict=verdict,
            grade=note,
            saving_eur=saving_vs_threshold(obs, config, item),
        )
        if ledger is not None:
            check = ledger.check_record(obs, config)
            offer.is_record = check.is_record
            offer.previous_best = check.previous_best
        (offers if is_reportable(verdict, obs) else pistes).append(offer)
    return offers, pistes


def build_drive_clients(
    config: Config,
    store_ids: list[str] | None = None,
    headless: bool = True,
    dry_run: bool = False,
    fixtures: dict[str, dict[str, list[DriveProduct]]] | None = None,
) -> dict[str, DriveClient]:
    """Un client par magasin doté d'un drive. Les fixtures court-circuitent le réseau."""
    clients: dict[str, DriveClient] = {}
    for store in config.drive_stores():
        if store_ids and store.id not in store_ids:
            continue
        if fixtures is not None:
            clients[store.id] = FixtureDriveClient(store, fixtures.get(store.id, {}), dry_run=dry_run)
            continue
        try:
            clients[store.id] = get_client(store.banner, store, headless=headless, dry_run=dry_run)
        except Exception as exc:
            log.info("pas de client drive pour %s : %s", store.name, exc)
    return clients


# --------------------------------------------------------------------------- #
# Run complet
# --------------------------------------------------------------------------- #
def run(
    config: Config | None = None,
    *,
    item_ids: list[str] | None = None,
    observations: list[PriceObservation] | None = None,
    manual_file: Path | str | None = None,
    pickup_date: date | None = None,
    use_drive: bool = True,
    offline: bool = False,
    headless: bool = True,
    fixtures: dict[str, dict[str, list[DriveProduct]]] | None = None,
    ledger_path: Path | str | None = None,
    report_dir: Path | str | None = None,
) -> RunResult:
    config = config or get_config()
    ledger = Ledger(ledger_path)
    run_id = ledger.start_run()

    collected: list[PriceObservation] = list(observations or [])
    if not collected:
        if manual_file:
            collected += load_observations(manual_file, config)
        collected += collect(config, item_ids, offline=offline)

    kept, _ = shortlist(collected, config, pickup_date)

    stats = VerificationStats()
    if use_drive and kept:
        clients = build_drive_clients(config, headless=headless, fixtures=fixtures)
        try:
            kept, stats = verify_in_drive(kept, config, clients)
        finally:
            for client in clients.values():
                try:
                    client.close()
                except Exception:
                    pass

    offers, pistes = build_offers(kept, config, ledger, pickup_date)
    plan = assign(offers, config)
    report = build_report(
        plan,
        config,
        pistes=pistes,
        observed_item_ids={o.basket_item_id for o in collected},
        pickup_date=pickup_date,
    )

    ledger.record_all([(o.observation, o.verdict) for o in offers + pistes])
    ledger.finish_run(run_id, len(collected), len(offers), stats.summary())

    if report_dir is not None:
        report.write(report_dir)

    ledger.close()
    return RunResult(plan, report, offers, pistes, collected, stats)


def fill_carts(
    plan: Plan,
    config: Config,
    *,
    dry_run: bool = True,
    headless: bool = False,
    store_ids: list[str] | None = None,
) -> dict[str, list]:
    """Remplit les paniers drive à partir du plan — et s'arrête là.

    Le choix du créneau et le paiement restent humains : c'est une contrainte
    fonctionnelle, pas une limite technique.
    """
    results: dict[str, list] = {}
    for basket in plan.baskets:
        if not basket.store.has_drive:
            continue
        if store_ids and basket.store.id not in store_ids:
            continue
        try:
            client = get_client(basket.store.banner, basket.store, headless=headless, dry_run=dry_run)
        except Exception as exc:
            log.warning("pas de client pour %s : %s", basket.store.name, exc)
            continue
        wanted = []
        for offer in basket.offers:
            obs = offer.observation
            wanted.append(
                (
                    DriveProduct(
                        ref=obs.drive_ref or obs.product_label,
                        label=obs.product_label,
                        price_eur=obs.price_eur,
                        url=obs.source_url,
                    ),
                    int(max(1, round(offer.item.qty_per_run))),
                )
            )
        try:
            results[basket.store.id] = client.fill(wanted)
        finally:
            client.close()
    return results

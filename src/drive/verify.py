"""Vérification en drive — LE filtre qui compte (constat C1).

Vérifié dans trois drives : sur les offres annoncées par les agrégateurs, la
majorité n'existe pas dans le drive correspondant (Prince 1,2 kg à 2,51 €,
Friskies 2 kg à 4,22 €, Ultima 9 kg à 26,95 €, Bigard 640 g à 9,99 € — tous
absents). Les prix de prospectus sont des prix MAGASIN.

Conséquence codée ici : le catalogue ne sert qu'à ORIENTER la recherche. Quand
le produit est retrouvé, c'est le prix du DRIVE qui remplace celui du
prospectus — jamais l'inverse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Config
from ..models import PriceObservation, Source
from .base import DriveClient, DriveError, best_match

log = logging.getLogger(__name__)


@dataclass
class VerificationStats:
    checked: int = 0
    found: int = 0
    absent: int = 0
    errors: int = 0

    @property
    def absence_rate(self) -> float:
        return self.absent / self.checked if self.checked else 0.0

    def summary(self) -> str:
        return (
            f"{self.found}/{self.checked} offres retrouvées en drive "
            f"({self.absence_rate:.0%} d'absentes), {self.errors} erreur(s)"
        )


def verify_observation(
    obs: PriceObservation, client: DriveClient, query: str | None = None
) -> PriceObservation:
    """Cherche le produit dans le drive et met l'observation à jour.

    Trouvé   → ``verified_in_drive=True``, prix et libellé remplacés par ceux du drive.
    Absent   → ``verified_in_drive=False`` : l'observation reste une piste.
    """
    search_terms = query or obs.product_label
    try:
        products = client.search(search_terms)
    except DriveError as exc:
        obs.notes.append(f"vérification impossible : {exc}")
        return obs

    match = best_match(products, obs.product_label)
    if match is None:
        obs.verified_in_drive = False
        obs.notes.append(
            f"absent du drive {client.banner} (prix prospectus = prix magasin)"
        )
        return obs

    obs.verified_in_drive = True
    obs.available = match.available
    obs.source = Source.DRIVE.value
    obs.drive_ref = match.ref
    if match.price_eur is not None and abs(match.price_eur - obs.price_eur) > 0.005:
        obs.notes.append(
            f"prix prospectus {obs.price_eur:.2f} € corrigé par le drive "
            f"{match.price_eur:.2f} €"
        )
        obs.price_eur = match.price_eur
        # Le libellé du drive fait foi : c'est lui qui porte le vrai grammage.
        obs.product_label = match.label
        obs.pack_size = None
        obs.pack_unit = None
        obs.pack_count = 1
    if match.url:
        obs.source_url = match.url
    obs.id = obs.fingerprint()
    return obs


def verify_in_drive(
    observations: list[PriceObservation],
    config: Config,
    clients: dict[str, DriveClient],
) -> tuple[list[PriceObservation], VerificationStats]:
    """Vérifie une liste d'observations, magasin par magasin.

    ``clients`` est indexé par ``store_id`` : un magasin sans client (Aldi,
    Lidl, Netto, Action — pas de drive) n'est pas vérifiable, ses observations
    restent des pistes et sortiront en liste papier.
    """
    stats = VerificationStats()
    for obs in observations:
        client = clients.get(obs.store_id)
        if client is None:
            continue
        stats.checked += 1
        try:
            verify_observation(obs, client)
        except Exception as exc:                       # un drive HS ne casse pas le run
            stats.errors += 1
            log.warning("vérification %s : %s", obs.store_id, exc)
            continue
        if obs.verified_in_drive:
            stats.found += 1
        else:
            stats.absent += 1

    if stats.checked and stats.absence_rate > 0.5:
        log.info(
            "%s — conforme au constat C1 : le catalogue n'est pas l'assortiment",
            stats.summary(),
        )
    return observations, stats

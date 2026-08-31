"""Socle commun aux collecteurs d'agrégateurs."""

from __future__ import annotations

import logging
import re
from datetime import datetime

from ..config import Config
from ..models import PriceObservation, Source
from ..units import strip_accents
from .html_extract import (
    Block,
    detect_loyalty_pct,
    detect_mechanic,
    detect_weight_basis,
    extract_jsonld,
    parse_all_prices,
    parse_price,
)
from .http import Fetcher, SourceBlocked

log = logging.getLogger(__name__)


class CollectorError(RuntimeError):
    """Le gabarit du site a changé : plus rien n'est extrait."""


def slugify(text: str) -> str:
    plain = strip_accents(text or "").lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")


class Collector:
    """Un collecteur produit des PISTES, jamais des offres (C1).

    ``verified_in_drive`` reste systématiquement à False en sortie de collecte.
    """

    name = "base"
    source = Source.AGGREGATOR.value

    def __init__(self, config: Config, fetcher: Fetcher | None = None):
        self.config = config
        self.fetcher = fetcher or Fetcher(config)
        self.settings = (config.sources.get("collectors", {}) or {}).get(self.name, {})

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True))

    # ------------------------------------------------------------------ #
    def collect(self, item_ids: list[str] | None = None) -> list[PriceObservation]:
        observations: list[PriceObservation] = []
        items = [
            self.config.items[i]
            for i in (item_ids or list(self.config.items))
            if i in self.config.items
        ]
        for item in items:
            if item.out_of_scope_drive:
                continue
            for query in (item.keywords or [item.label])[:2]:
                try:
                    observations += self.collect_one(item.id, query)
                except SourceBlocked as exc:
                    log.info("source écartée : %s", exc)
                except Exception as exc:  # un collecteur cassé ne casse pas le run
                    log.warning("%s a échoué sur %r : %s", self.name, query, exc)
        return observations

    def collect_one(self, item_id: str, query: str) -> list[PriceObservation]:
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    def make_observation(
        self,
        *,
        item_id: str,
        store_id: str,
        label: str,
        price: float,
        text: str = "",
        url: str | None = None,
        banner: str | None = None,
        regular_price: float | None = None,
    ) -> PriceObservation:
        item = self.config.items[item_id]
        return PriceObservation(
            store_id=store_id,
            basket_item_id=item_id,
            product_label=label,
            price_eur=price,
            regular_price=regular_price,
            category=item.category,
            observed_at=datetime.now(),
            mechanic=detect_mechanic(text or label),
            weight_basis=detect_weight_basis(text or label),
            loyalty_pct=detect_loyalty_pct(text or label),
            source=self.source,
            verified_in_drive=False,       # invariant : la collecte ne vérifie rien
            source_url=url,
            banner=banner,
        )

    # ------------------------------------------------------------------ #
    def store_for_banner(self, banner: str) -> str | None:
        """Rattache une enseigne à un magasin du référentiel, exclusions comprises."""
        banner = (banner or "").lower()
        if not banner or self.config.is_excluded(banner):
            return None
        candidates = [
            s for s in self.config.allowed_stores() if s.banner.lower() == banner
        ]
        if not candidates:
            return None
        # À enseigne égale, le magasin le moins coûteux en détour.
        candidates.sort(key=lambda s: (s.detour_km, s.distance_km))
        return candidates[0].id

    def guess_banner(self, text: str) -> str | None:
        plain = strip_accents(text or "").lower()
        known = {
            "leclerc": "leclerc",
            "intermarche": "intermarche",
            "super u": "u",
            "hyper u": "u",
            "u express": "u",
            "lidl": "lidl",
            "aldi": "aldi",
            "netto": "netto",
            "action": "action",
            "grand frais": "grandfrais",
            "maxi zoo": "maxizoo",
            "carrefour": "carrefour",
            "auchan": "auchan",
        }
        for needle, banner in known.items():
            if needle in plain:
                return banner
        return None

    @staticmethod
    def regular_and_promo(text: str) -> tuple[float | None, float | None]:
        """Sépare « au lieu de X » du prix affiché.

        Sans cette lecture, les règles P1 et P2 n'ont rien à mordre.
        """
        prices = parse_all_prices(text)
        if not prices:
            return None, None
        if len(prices) == 1:
            return None, prices[0]
        promo = min(prices)
        regular = max(prices)
        return (regular if regular > promo else None), promo

    @staticmethod
    def blocks_text(blocks: list[Block]) -> str:
        return " | ".join(b.text for b in blocks)

    @staticmethod
    def jsonld_offers(html: str) -> list[dict]:
        return [
            node
            for node in extract_jsonld(html)
            if str(node.get("@type", "")).lower() in {"product", "offer", "aggregateoffer"}
        ]

    @staticmethod
    def price_from_jsonld(node: dict) -> float | None:
        offers = node.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        for key in ("price", "lowPrice"):
            value = (offers or {}).get(key) if isinstance(offers, dict) else None
            if value is None:
                value = node.get(key)
            if value is not None:
                try:
                    return float(str(value).replace(",", "."))
                except ValueError:
                    continue
        return parse_price(str(node.get("description", "")))

"""promocatalogues.fr — la source la plus productive (§6).

⚠ Le slash final de ``/offres/<produit>/`` est significatif : sans lui la page
ne renvoie rien d'exploitable.

Rappel : ce que produit ce collecteur est une piste. Les prix des prospectus
sont des prix MAGASIN, et la majorité des offres annoncées n'existent pas dans
le drive correspondant (constat C1).
"""

from __future__ import annotations

import logging

from ..models import PriceObservation
from .base import Collector, slugify
from .html_extract import find_blocks, parse_price

log = logging.getLogger(__name__)


class PromocataloguesCollector(Collector):
    name = "promocatalogues"

    def offer_url(self, query: str) -> str:
        template = self.settings.get(
            "offer_url_template", "https://www.promocatalogues.fr/offres/{slug}/"
        )
        url = template.format(slug=slugify(query))
        if not url.endswith("/"):
            url += "/"          # le slash final, encore
        return url

    def catalog_url(self, banner: str) -> str | None:
        template = self.settings.get("catalog_url_template")
        slug = (self.settings.get("banner_slugs", {}) or {}).get(banner, banner)
        return template.format(banner=slug) if template else None

    # ------------------------------------------------------------------ #
    def collect_one(self, item_id: str, query: str) -> list[PriceObservation]:
        url = self.offer_url(query)
        response = self.fetcher.get(url)
        if response.status != 200 or not response.text:
            return []
        return self.parse(response.text, item_id, url)

    def parse(self, html: str, item_id: str, url: str) -> list[PriceObservation]:
        """Extrait les offres d'une page produit.

        JSON-LD d'abord, blocs de cartes ensuite. Si les deux échouent alors
        que la page a du contenu, on le journalise : une liste vide qui passe
        pour « pas de promo » est le pire des résultats.
        """
        observations = [
            *self._from_jsonld(html, item_id, url),
        ]
        if not observations:
            observations = self._from_cards(html, item_id, url)
        if not observations and len(html) > 2000:
            log.warning("promocatalogues : aucune offre extraite de %s (gabarit changé ?)", url)
        return observations

    # ------------------------------------------------------------------ #
    def _from_jsonld(self, html: str, item_id: str, url: str) -> list[PriceObservation]:
        observations: list[PriceObservation] = []
        for node in self.jsonld_offers(html):
            label = str(node.get("name") or "").strip()
            price = self.price_from_jsonld(node)
            if not label or price is None:
                continue
            text = f"{label} {node.get('description', '')}"
            banner = self.guess_banner(text) or self.guess_banner(str(node.get("brand", "")))
            store_id = self.store_for_banner(banner) if banner else None
            if store_id is None:
                continue            # enseigne exclue ou hors corridor : on jette
            observations.append(
                self.make_observation(
                    item_id=item_id,
                    store_id=store_id,
                    label=label,
                    price=price,
                    text=text,
                    url=url,
                    banner=banner,
                )
            )
        return observations

    def _from_cards(self, html: str, item_id: str, url: str) -> list[PriceObservation]:
        observations: list[PriceObservation] = []
        blocks = find_blocks(html, "div", "offer") or find_blocks(html, "article", None)
        for block in blocks:
            text = block.text
            if not text or len(text) < 8:
                continue
            regular, promo = self.regular_and_promo(text)
            price = promo if promo is not None else parse_price(text)
            if price is None:
                continue
            banner = self.guess_banner(text)
            store_id = self.store_for_banner(banner) if banner else None
            if store_id is None:
                continue
            label = text[:120]
            observations.append(
                self.make_observation(
                    item_id=item_id,
                    store_id=store_id,
                    label=label,
                    price=price,
                    text=text,
                    url=block.links[0] if block.links else url,
                    banner=banner,
                    regular_price=regular,
                )
            )
        return observations

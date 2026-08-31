"""vos-promos.fr — source secondaire, même contrat que promocatalogues."""

from __future__ import annotations

from ..models import PriceObservation
from .base import Collector, slugify
from .html_extract import find_blocks, parse_price


class VosPromosCollector(Collector):
    name = "vospromos"

    def offer_url(self, query: str) -> str:
        template = self.settings.get(
            "offer_url_template", "https://www.vos-promos.fr/produits/{slug}"
        )
        return template.format(slug=slugify(query))

    def collect_one(self, item_id: str, query: str) -> list[PriceObservation]:
        url = self.offer_url(query)
        response = self.fetcher.get(url)
        if response.status != 200 or not response.text:
            return []
        return self.parse(response.text, item_id, url)

    def parse(self, html: str, item_id: str, url: str) -> list[PriceObservation]:
        observations: list[PriceObservation] = []
        blocks = (
            find_blocks(html, "div", "product")
            or find_blocks(html, "li", "promo")
            or find_blocks(html, "article", None)
        )
        for block in blocks:
            text = block.text
            regular, promo = self.regular_and_promo(text)
            price = promo if promo is not None else parse_price(text)
            if price is None:
                continue
            banner = self.guess_banner(text)
            store_id = self.store_for_banner(banner) if banner else None
            if store_id is None:
                continue
            observations.append(
                self.make_observation(
                    item_id=item_id,
                    store_id=store_id,
                    label=text[:120],
                    price=price,
                    text=text,
                    url=url,
                    banner=banner,
                    regular_price=regular,
                )
            )
        return observations

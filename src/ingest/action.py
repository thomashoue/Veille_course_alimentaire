"""action.com — prix PERMANENTS, pas des promos.

Action n'a pas de drive : ce collecteur ne produit donc jamais d'offre
actionnable en drive, mais ses prix servent de référence droguerie (le papier
toilette à 0,207 €/rouleau est imbattable et doit sortir en liste papier).
"""

from __future__ import annotations

from ..models import PriceObservation, Source
from .base import Collector
from .html_extract import find_blocks, parse_price


class ActionCollector(Collector):
    name = "action"
    source = Source.CATALOGUE.value

    def search_url(self, query: str) -> str:
        from urllib.parse import quote

        template = self.settings.get(
            "search_url_template", "https://www.action.com/fr-fr/search/?q={q}"
        )
        return template.format(q=quote(query))

    def collect_one(self, item_id: str, query: str) -> list[PriceObservation]:
        url = self.search_url(query)
        response = self.fetcher.get(url)
        if response.status != 200 or not response.text:
            return []
        return self.parse(response.text, item_id, url)

    def parse(self, html: str, item_id: str, url: str) -> list[PriceObservation]:
        store_id = self.store_for_banner("action")
        if store_id is None:
            return []
        observations: list[PriceObservation] = []
        for node in self.jsonld_offers(html):
            label = str(node.get("name") or "").strip()
            price = self.price_from_jsonld(node)
            if label and price is not None:
                observations.append(
                    self._observation(item_id, store_id, label, price, url)
                )
        if observations:
            return observations
        for block in find_blocks(html, "div", "product"):
            price = parse_price(block.text)
            if price is None:
                continue
            observations.append(
                self._observation(item_id, store_id, block.text[:120], price, url)
            )
        return observations

    def _observation(
        self, item_id: str, store_id: str, label: str, price: float, url: str
    ) -> PriceObservation:
        obs = self.make_observation(
            item_id=item_id,
            store_id=store_id,
            label=label,
            price=price,
            text=label,
            url=url,
            banner="action",
        )
        # Prix permanent en magasin : pas de drive à vérifier, mais l'article
        # reste actionnable en liste papier. Cf. §10.
        obs.notes.append("prix permanent Action — achat en magasin, pas de drive")
        return obs

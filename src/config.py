"""Chargement des YAML de config/ — §8.

Rien de ce qui est calibré (seuils, corridors, coût du kilomètre) ne doit vivre
dans le code : tout passe par ici.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .models import BasketItem, Store

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("VEILLE_CONFIG_DIR", ROOT / "config"))
DATA_DIR = Path(os.environ.get("VEILLE_DATA_DIR", ROOT / "data"))


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"config manquante : {path}")
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


@dataclass
class Config:
    stores: dict[str, Store]
    excluded_banners: set[str]
    items: dict[str, BasketItem]
    thresholds: dict[str, dict[str, Any]]
    params: dict[str, Any]
    sources: dict[str, Any]
    out_of_scope_stores: list[str] = field(default_factory=list)

    # -- magasins ----------------------------------------------------------- #
    def store(self, store_id: str) -> Store:
        return self.stores[store_id]

    def is_excluded(self, store_id_or_banner: str) -> bool:
        """Filtre d'exclusion dur (Carrefour / Auchan), par id OU par enseigne."""
        key = (store_id_or_banner or "").lower()
        if key in self.excluded_banners:
            return True
        store = self.stores.get(store_id_or_banner)
        if store is None:
            return False
        return store.excluded or store.banner.lower() in self.excluded_banners

    def allowed_stores(self) -> list[Store]:
        return [s for s in self.stores.values() if not self.is_excluded(s.id)]

    def drive_stores(self) -> list[Store]:
        return [s for s in self.allowed_stores() if s.has_drive]

    # -- panier ------------------------------------------------------------- #
    def item(self, item_id: str) -> BasketItem:
        return self.items[item_id]

    def match_item(
        self, label: str, *, include_out_of_scope: bool = False
    ) -> BasketItem | None:
        """Rattache un libellé de drive à un article du panier, par mot-clé.

        Deux garde-fous appris sur données réelles Intermarché :
          * frontières de mot — « riz » ne doit pas matcher « chorizo », ni
            « oignon » le fromage « Soignon », ni « banane » un yaourt à boire ;
          * les fruits/légumes (hors périmètre drive) ne sont pas cherchés ici,
            sauf demande explicite.

        Le mot-clé le plus long qui matche gagne (« croquettes chat » >
        « croquettes »).
        """
        haystack = _normalize(label or "")
        best: tuple[int, BasketItem] | None = None
        for item in self.items.values():
            if item.out_of_scope_drive and not include_out_of_scope:
                continue
            if any(_contains_word(haystack, excluded) for excluded in item.exclude_keywords):
                continue
            for keyword in item.keywords:
                if _contains_word(haystack, keyword):
                    score = len(_normalize(keyword))
                    if best is None or score > best[0]:
                        best = (score, item)
        return best[1] if best else None

    # -- seuils ------------------------------------------------------------- #
    def threshold(self, item_id: str, attributes: dict[str, str] | None = None) -> dict[str, Any]:
        """Seuils d'un article, spécialisés par attribut quand c'est prévu.

        La litière n'a pas le même seuil en silice (1,30 €/L) et en agglomérante
        charbon (0,92 €/L) : c'est ``by_attribute`` qui porte cette distinction.
        """
        base = dict(self.thresholds.get(item_id, {}))
        by_attribute = base.pop("by_attribute", None)
        if by_attribute and attributes:
            for attr_name, mapping in by_attribute.items():
                value = attributes.get(attr_name)
                if value and value in mapping:
                    base.update(mapping[value])
        return base

    def param(self, name: str, default: Any = None) -> Any:
        return self.params.get(name, default)


# Rattachement par mot entier, insensible aux accents. Un mot-clé multi-mot
# (« steak haché ») est cherché comme une expression, bornée aux deux bouts.
import re as _re

from .units import strip_accents as _strip


def _normalize(text: str) -> str:
    return _strip(text or "").lower()


def _contains_word(haystack: str, keyword: str) -> bool:
    needle = _normalize(keyword)
    if not needle:
        return False
    # Pluriel français toléré (rongeur→rongeurs, oignon→oignons), mais la
    # frontière tient : « riz » ne matche pas « chorizo », « oignon » pas
    # « Soignon ».
    return _re.search(
        rf"(?<![a-z0-9]){_re.escape(needle)}(?:s|x)?(?![a-z0-9])", haystack
    ) is not None


def _store_from_dict(raw: dict[str, Any]) -> Store:
    known = {f for f in Store.__dataclass_fields__}
    return Store(**{k: v for k, v in raw.items() if k in known})


def _item_from_dict(raw: dict[str, Any]) -> BasketItem:
    known = {f for f in BasketItem.__dataclass_fields__}
    return BasketItem(**{k: v for k, v in raw.items() if k in known})


def load_config(config_dir: Path | str | None = None) -> Config:
    directory = Path(config_dir) if config_dir else CONFIG_DIR

    stores_raw = _load_yaml(directory / "stores.yaml")
    basket_raw = _load_yaml(directory / "basket.yaml")
    thresholds_raw = _load_yaml(directory / "thresholds.yaml")
    sources_raw = _load_yaml(directory / "sources.yaml")

    stores = {s["id"]: _store_from_dict(s) for s in stores_raw.get("stores", [])}
    excluded = {b.lower() for b in stores_raw.get("excluded_banners", [])}
    for store in stores.values():
        if store.banner.lower() in excluded:
            store.excluded = True

    items = {i["id"]: _item_from_dict(i) for i in basket_raw.get("items", [])}

    return Config(
        stores=stores,
        excluded_banners=excluded,
        items=items,
        thresholds=thresholds_raw.get("thresholds", {}),
        params=thresholds_raw.get("params", {}),
        sources=sources_raw,
        out_of_scope_stores=basket_raw.get("out_of_scope_stores", []),
    )


@lru_cache(maxsize=4)
def _cached(directory: str) -> Config:
    return load_config(directory)


def get_config(config_dir: Path | str | None = None) -> Config:
    """Config partagée par le pipeline (mise en cache par répertoire)."""
    return _cached(str(Path(config_dir) if config_dir else CONFIG_DIR))

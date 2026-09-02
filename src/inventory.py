"""Inventaire du garde-manger : stock = acheté − consommé.

Hypothèse de départ : stock à zéro. Le menu consomme (les recettes retirent
leurs ingrédients), les courses réapprovisionnent (ajouts), et la liste à
acheter ne réclame que le manquant = besoin − stock.

Le stock est suivi par couple (article du panier, unité) : le riz en kg, les
conserves en boîtes, les œufs à l'unité. Pas de conversion entre unités — on
reste dans l'unité où l'on achète et où l'on cuisine, qui est la même.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import DATA_DIR, Config


def _path() -> Path:
    return DATA_DIR / "inventory.json"


@dataclass
class Movement:
    date: str
    item: str
    qty: float          # + achat, − consommation
    unit: str
    reason: str = ""


class Inventory:
    """Mouvements de stock, persistés en JSON (local, comme le ledger)."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else _path()
        self.movements: list[Movement] = []
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.movements = [Movement(**m) for m in raw]
            except (json.JSONDecodeError, ValueError, TypeError):
                self.movements = []

    # ------------------------------------------------------------------ #
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([m.__dict__ for m in self.movements], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, item: str, qty: float, unit: str, reason: str = "achat") -> None:
        self.movements.append(Movement(date.today().isoformat(), item, float(qty), unit, reason))

    def consume(self, item: str, qty: float, unit: str, reason: str = "repas") -> None:
        self.movements.append(Movement(date.today().isoformat(), item, -abs(float(qty)), unit, reason))

    def reset(self) -> None:
        self.movements = []

    # ------------------------------------------------------------------ #
    def current(self) -> dict[tuple[str, str], float]:
        """Stock courant par (article, unité). Les zéros exacts sont retirés."""
        stock: dict[tuple[str, str], float] = {}
        for m in self.movements:
            key = (m.item, m.unit)
            stock[key] = round(stock.get(key, 0.0) + m.qty, 3)
        return {k: v for k, v in stock.items() if abs(v) > 1e-6}

    def by_item(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for (item, unit), qty in self.current().items():
            out.setdefault(item, {})[unit] = qty
        return out


# --------------------------------------------------------------------------- #
def consume_menu(inventory: Inventory, config: Config, recipes, servings: int | None = None) -> None:
    """Retranche du stock les ingrédients d'une semaine cuisinée.

    Seuls les articles suivis au panier sont décomptés (le frais, périssable,
    n'est pas stocké).
    """
    scale = (servings or config.servings_base) / config.servings_base
    for recipe in recipes:
        for ing in recipe.ingredients:
            if ing.basket_item and ing.basket_item in config.items:
                inventory.consume(
                    ing.basket_item, ing.qty * scale, ing.unit, reason=f"repas:{recipe.id}"
                )

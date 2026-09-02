"""Menu de la semaine → liste de courses par BESOIN.

On part des repas, pas des habitudes : `plan_week` choisit 7 dîners variés
(sous contraintes : poisson limité, protéines diversifiées, pas de répétition
récente), puis `shopping_list` agrège leurs ingrédients — c'est la liste réelle
à acheter, reliée à la veille prix.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .config import DATA_DIR, Config
from .models import Recipe

JOURS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]


@dataclass
class Line:
    label: str
    qty: float                       # à acheter (besoin − stock)
    unit: str
    basket_item: str | None = None
    category: str = ""
    from_recipes: list[str] = field(default_factory=list)
    need: float = 0.0                # besoin total avant déduction du stock
    in_stock: float = 0.0            # quantité déjà en stock


@dataclass
class Menu:
    week: list[tuple[str, Recipe]]        # (jour, recette)
    to_buy: list[Line]                    # articles reliés au panier (veille prix)
    fresh: list[Line]                     # frais : marché / Grand Frais
    pantry: list[Line]                    # épicerie hors panier suivi
    covered: list[Line] = field(default_factory=list)  # besoin couvert par le stock

    def all_lines(self) -> list[Line]:
        return self.to_buy + self.fresh + self.pantry


# --------------------------------------------------------------------------- #
def _history_path() -> Path:
    return DATA_DIR / "menu_history.json"


def _recent_ids(weeks: int = 2) -> set[str]:
    """Recettes des dernières semaines, à éviter pour varier."""
    path = _history_path()
    if not path.exists():
        return set()
    try:
        hist = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return set()
    recent: set[str] = set()
    for entry in hist[-weeks:]:
        recent.update(entry.get("recipes", []))
    return recent


def record_week(recipe_ids: list[str]) -> None:
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    hist = []
    if path.exists():
        try:
            hist = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            hist = []
    hist.append({"date": date.today().isoformat(), "recipes": recipe_ids})
    path.write_text(json.dumps(hist[-12:], ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
def plan_week(
    config: Config,
    *,
    n: int = 7,
    fish_max: int = 2,
    seed: int | None = None,
    avoid_recent: bool = True,
    tags_required: list[str] | None = None,
) -> list[Recipe]:
    """Choisit n dîners : poisson plafonné, protéines variées, sans répétition.

    L'équilibre de la semaine passe avant le hasard : on remplit d'abord une
    protéine de chaque type, puis on complète en évitant de refaire trois fois
    le même registre.
    """
    rng = random.Random(seed)
    pool = list(config.recipes.values())
    if tags_required:
        pool = [r for r in pool if all(r.has_tag(t) for t in tags_required)]

    recent = _recent_ids() if avoid_recent else set()
    fresh_pool = [r for r in pool if r.id not in recent]
    if len(fresh_pool) >= n:
        pool = fresh_pool                      # assez de nouveauté : on évite les récentes

    rng.shuffle(pool)
    chosen: list[Recipe] = []
    fish = 0
    used_proteins: dict[str, int] = defaultdict(int)

    # 1er passage : diversifier les protéines, respecter le plafond poisson.
    for recipe in pool:
        if len(chosen) >= n:
            break
        is_fish = recipe.proteine == "poisson"
        if is_fish and fish >= fish_max:
            continue
        if used_proteins[recipe.proteine] >= 3:   # pas plus de 3 fois le même registre
            continue
        chosen.append(recipe)
        used_proteins[recipe.proteine] += 1
        fish += 1 if is_fish else 0

    # 2e passage : compléter si besoin (contraintes assouplies sauf poisson).
    if len(chosen) < n:
        for recipe in pool:
            if len(chosen) >= n:
                break
            if recipe in chosen:
                continue
            if recipe.proteine == "poisson" and fish >= fish_max:
                continue
            chosen.append(recipe)
            fish += 1 if recipe.proteine == "poisson" else 0

    return chosen[:n]


# --------------------------------------------------------------------------- #
def shopping_list(
    config: Config,
    recipes: list[Recipe],
    servings: int | None = None,
    stock: dict[tuple[str, str], float] | None = None,
) -> Menu:
    """Agrège les ingrédients des recettes en une liste de courses.

    Quantités mises à l'échelle des convives, puis réparties en trois listes :
      * `to_buy`  — reliées au panier suivi (drive, comparaison prix) ;
      * `fresh`   — fruits/légumes/poisson frais (marché, Grand Frais, Ecomiam) ;
      * `pantry`  — épicerie hors panier suivi (lait de coco, semoule…).
    """
    scale = (servings or config.servings_base) / config.servings_base
    agg: dict[tuple[str, str, str], Line] = {}

    for recipe in recipes:
        for ing in recipe.ingredients:
            key = (ing.basket_item or ing.label.lower(), ing.unit, ing.category)
            line = agg.get(key)
            if line is None:
                line = Line(
                    label=ing.label,
                    qty=0.0,
                    unit=ing.unit,
                    basket_item=ing.basket_item,
                    category=ing.category,
                )
                agg[key] = line
            line.qty += ing.qty * scale
            line.need += ing.qty * scale
            line.from_recipes.append(recipe.id)

    stock = stock or {}
    to_buy, fresh, pantry, covered = [], [], [], []
    for line in agg.values():
        line.need = round(line.need, 2)
        # Déduire le stock (besoin − stock) pour les articles suivis.
        if line.basket_item:
            have = stock.get((line.basket_item, line.unit), 0.0)
            line.in_stock = round(have, 2)
            line.qty = round(max(0.0, line.need - have), 2)
        else:
            line.qty = line.need
        if line.category in ("fl", "poisson"):
            fresh.append(line)          # frais : non stocké, on achète le besoin
        elif line.basket_item and line.basket_item in config.items:
            if line.qty <= 0:
                covered.append(line)    # entièrement en stock
            else:
                to_buy.append(line)
        else:
            pantry.append(line)

    to_buy.sort(key=lambda l: config.items[l.basket_item].category if l.basket_item in config.items else "")
    fresh.sort(key=lambda l: l.label)
    pantry.sort(key=lambda l: l.label)
    covered.sort(key=lambda l: l.label)
    week = [(JOURS[i], r) for i, r in enumerate(recipes)]
    return Menu(week=week, to_buy=to_buy, fresh=fresh, pantry=pantry, covered=covered)

"""Normalisation : prix au format → prix comparable.

Constat C3 : rien n'est comparable tel qu'affiché. Ce module produit
``unit_price`` (prix du pack ramené à l'unité de base) puis
``effective_unit_price`` (prix moyen sur la quantité RÉELLEMENT achetée,
mécanique promotionnelle et avantage carte inclus).

Aucune de ces deux valeurs n'est jamais saisie : elles sont calculées ici.
"""

from __future__ import annotations

from datetime import date

from . import units
from .config import Config
from .models import MECHANIC_SECOND_DISCOUNTS, BasketItem, PriceObservation


# --------------------------------------------------------------------------- #
# Attributs (litière silice / charbon, vinaigre bidon / spray…)
# --------------------------------------------------------------------------- #
def infer_attributes(label: str, item: BasketItem) -> dict[str, str]:
    """Déduit les attributs d'un produit depuis son libellé.

    L'ordre de déclaration dans ``attribute_rules`` fait foi : « bentonite au
    charbon actif » doit être vue comme ``agglo_charbon`` et non ``minerale``.
    """
    found: dict[str, str] = {}
    haystack = units.strip_accents(label or "").lower()
    for attr_name, values in (item.attribute_rules or {}).items():
        for value, keywords in values.items():
            if any(units.strip_accents(k).lower() in haystack for k in keywords):
                found[attr_name] = value
                break
    return found


# --------------------------------------------------------------------------- #
# Conditionnement
# --------------------------------------------------------------------------- #
def resolve_pack(obs: PriceObservation) -> units.Pack | None:
    """Conditionnement de l'observation, lu du champ ou à défaut du libellé."""
    if obs.pack_size is not None and obs.pack_unit:
        try:
            return units.Pack(obs.pack_size, units.canonical_unit(obs.pack_unit), obs.pack_count)
        except units.UnknownUnit:
            return None
    pack = units.parse_pack(obs.product_label)
    if pack is not None:
        obs.pack_size, obs.pack_unit, obs.pack_count = pack.size, pack.unit, pack.count
    return pack


def drained_quantity(quantity: float, obs: PriceObservation, config: Config) -> tuple[float, bool]:
    """Ramène une quantité au net égoutté quand la conversion est connue (P5).

    Renvoie ``(quantité, converti)``. Sans ratio configuré on ne convertit pas :
    mieux vaut un FLAG qu'un €/kg inventé.
    """
    if obs.weight_basis != "brut":
        return quantity, False
    ratios = config.param("drained_ratios", {}) or {}
    ratio = ratios.get(obs.category)
    if not ratio:
        return quantity, False
    return quantity * float(ratio), True


# --------------------------------------------------------------------------- #
# Mécaniques promotionnelles
# --------------------------------------------------------------------------- #
def required_quantity(mechanic: str | None) -> int:
    """Nombre d'unités à acheter pour que la mécanique s'applique."""
    if not mechanic:
        return 1
    if mechanic.startswith("second_"):
        return 2
    if mechanic == "3_pour_2":
        return 3
    return 1


def total_for_required_qty(price: float, mechanic: str | None) -> float:
    """Ce qu'on paie réellement pour la quantité imposée par la mécanique.

    Contre-exemple vécu (P3) : 4,43 € le 1er sac + 3,10 € le 2ᵉ à −30 %,
    soit 7,53 € pour deux sacs — et non 2 × 3,10 €.
    """
    if not mechanic:
        return price
    if mechanic.startswith("second_"):
        discount = MECHANIC_SECOND_DISCOUNTS.get(mechanic)
        if discount is None:
            return price
        return price + price * (1.0 - discount)
    if mechanic == "3_pour_2":
        return 2.0 * price
    return price


def mean_over_required_qty(obs: PriceObservation, quantity_base: float) -> float:
    """Prix unitaire moyen sur la quantité réellement achetée (P3)."""
    qty = required_quantity(obs.mechanic)
    total = total_for_required_qty(obs.price_eur, obs.mechanic)
    return total / (qty * quantity_base)


# --------------------------------------------------------------------------- #
# Avantage carte
# --------------------------------------------------------------------------- #
def loyalty_applies(obs: PriceObservation, pickup_date: date | None) -> bool:
    """L'avantage carte est conditionné à la date de RETRAIT, pas de commande."""
    if not obs.loyalty_pct:
        return False
    if obs.loyalty_valid_until is None:
        return True
    if pickup_date is None:
        return True
    return pickup_date <= obs.loyalty_valid_until


# --------------------------------------------------------------------------- #
# Entrée principale
# --------------------------------------------------------------------------- #
def normalize(
    obs: PriceObservation,
    config: Config,
    item: BasketItem | None = None,
    pickup_date: date | None = None,
) -> PriceObservation:
    """Complète l'observation : attributs, pack, prix unitaire, prix effectif.

    Ne juge pas : c'est le rôle de :mod:`src.validate`. Ici on se contente de
    rendre l'observation comparable, ou de laisser les champs à ``None`` quand
    c'est impossible (format absent, unité inconnue).
    """
    item = item or config.items.get(obs.basket_item_id)
    if item is not None:
        if not obs.category:
            obs.category = item.category
        merged = infer_attributes(obs.product_label, item)
        merged.update(obs.attributes)   # un attribut fourni explicitement prime
        obs.attributes = merged

    store = config.stores.get(obs.store_id)
    if store is not None:
        if not obs.banner:
            obs.banner = store.banner
        obs.requires_drive_verification = store.has_drive

    pack = resolve_pack(obs)
    if pack is None:
        # P4 : sans grammage, interdiction de calculer un prix au kilo.
        obs.unit_price = None
        obs.effective_unit_price = None
        obs.unit_price_unit = None
        obs.required_qty = required_quantity(obs.mechanic)
        return obs

    quantity = pack.total_base
    quantity, converted = drained_quantity(quantity, obs, config)
    if converted:
        obs.notes.append(
            f"poids brut converti en net égoutté (ratio {config.param('drained_ratios')[obs.category]})"
        )

    # Dans la famille « compte », l'unité de base ('unite') n'apprend rien :
    # on garde celle du conditionnement, qui est celle du seuil (€/rouleau…).
    obs.unit_price_unit = (
        units.canonical_unit(pack.unit)
        if units.family(pack.unit) == "count"
        else units.base_unit(pack.unit)
    )
    obs.unit_price = obs.price_eur / quantity
    obs.required_qty = required_quantity(obs.mechanic)
    effective = mean_over_required_qty(obs, quantity)

    if loyalty_applies(obs, pickup_date):
        effective *= 1.0 - float(obs.loyalty_pct) / 100.0
        obs.notes.append(f"avantage carte {obs.loyalty_pct:g} % appliqué")
    elif obs.loyalty_pct:
        obs.loyalty_pct = 0.0
        obs.notes.append("avantage carte ignoré : hors fenêtre de retrait")

    obs.effective_unit_price = effective
    return obs


def normalize_all(
    observations: list[PriceObservation],
    config: Config,
    pickup_date: date | None = None,
) -> list[PriceObservation]:
    return [normalize(obs, config, pickup_date=pickup_date) for obs in observations]

"""Modèle de données — §3 de la spec.

Invariant central : ``verified_in_drive is False`` ⇒ l'observation peut être
stockée comme piste, mais ne peut pas entrer dans un compte rendu comme offre.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any

from . import units


# --------------------------------------------------------------------------- #
# Référentiel
# --------------------------------------------------------------------------- #
@dataclass
class Store:
    id: str
    banner: str
    name: str
    city: str = ""
    postcode: str = ""
    corridor: str = "home"          # 'home' | 'east' | 'west'
    assignee: str = "household"     # 'household' | 'charlotte' | 'thomas'
    distance_km: float = 0.0
    detour_km: float = 0.0
    has_drive: bool = False
    drive_base_url: str | None = None
    search_url_template: str | None = None
    cart_url_template: str | None = None
    format: str = "super"
    min_order_eur: float | None = None   # minimum de commande du drive
    excluded: bool = False

    def search_url(self, query: str) -> str | None:
        if not self.search_url_template:
            return None
        from urllib.parse import quote

        return self.search_url_template.format(
            base=(self.drive_base_url or "").rstrip("/"), q=quote(query)
        )

    def cart_url(self) -> str | None:
        if not self.cart_url_template:
            return None
        return self.cart_url_template.format(base=(self.drive_base_url or "").rstrip("/"))


@dataclass
class BasketItem:
    id: str
    label: str
    category: str
    unit: str                                   # unité de comparaison
    bulk_worthy: bool = False
    keywords: list[str] = field(default_factory=list)
    # Si l'un de ces mots apparaît dans un libellé, le produit n'est PAS cet
    # article — « litière rongeurs » n'est pas une litière pour chat.
    exclude_keywords: list[str] = field(default_factory=list)
    hard_constraints: list[str] = field(default_factory=list)
    attribute_rules: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    qty_per_run: float = 1.0       # quantité d'un run, dans l'unité de l'article
    qty_stock: float | None = None  # quantité visée quand on stocke (bulk_worthy)
    out_of_scope_drive: bool = False
    weight_basis_required: bool = False
    fallback_advice: str | None = None

    @property
    def base_unit(self) -> str:
        return units.base_unit(self.unit)


# --------------------------------------------------------------------------- #
# Relevé de prix
# --------------------------------------------------------------------------- #
class Source(str, Enum):
    DRIVE = "drive"
    CATALOGUE = "catalogue"
    AGGREGATOR = "aggregator"


MECHANIC_SECOND_DISCOUNTS = {
    "second_-30": 0.30,
    "second_-50": 0.50,
    "second_-60": 0.60,
    "second_-70": 0.70,
}


@dataclass
class PriceObservation:
    """Un prix relevé quelque part, pour un article du panier.

    ``unit_price`` et ``effective_unit_price`` sont CALCULÉS par
    :mod:`src.normalize` — ne jamais les saisir à la main.
    """

    store_id: str
    basket_item_id: str
    product_label: str
    price_eur: float

    id: str | None = None
    observed_at: datetime = field(default_factory=datetime.now)
    category: str = ""
    pack_size: float | None = None
    pack_unit: str | None = None
    pack_count: int = 1
    regular_price: float | None = None

    unit_price: float | None = None            # calculé
    unit_price_unit: str | None = None         # unité de unit_price ('kg', 'L'…)
    effective_unit_price: float | None = None  # calculé, mécanique incluse
    required_qty: int = 1                      # quantité imposée par la mécanique

    weight_basis: str | None = None            # 'brut' | 'net_egoutte'
    mechanic: str | None = None
    loyalty_pct: float | None = None
    loyalty_valid_until: date | None = None
    valid_from: date | None = None
    valid_until: date | None = None

    source: str = Source.AGGREGATOR.value
    verified_in_drive: bool = False
    # Aldi, Lidl, Netto, Action et Grand Frais n'ont pas de drive (§10) : il n'y
    # a rien à y vérifier, et le prix du prospectus EST le prix magasin qu'on
    # paiera. Ces observations sortent en liste papier, pas en panier drive.
    requires_drive_verification: bool = True
    source_url: str | None = None
    banner: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Incohérence détectée à la lecture (ex. prix du pack égal au prix au
    # litre affiché) : l'observation sort en « à vérifier », jamais en offre.
    suspect_reason: str | None = None
    drive_ref: str | None = None               # référence produit côté drive
    available: bool = True

    def __post_init__(self) -> None:
        if self.id is None:
            self.id = self.fingerprint()

    def fingerprint(self) -> str:
        """Identité stable d'une observation, pour dédoublonner le ledger."""
        raw = "|".join(
            str(x)
            for x in (
                self.store_id,
                self.basket_item_id,
                self.product_label.strip().lower(),
                f"{self.price_eur:.2f}",
                self.pack_size,
                self.pack_unit,
                self.mechanic or "",
                self.observed_at.date().isoformat(),
            )
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def is_actionable(self) -> bool:
        """Seul critère qui compte (C1) : vu dans le drive, et disponible.

        Seule exception, explicite : une enseigne sans drive, où il n'y a rien
        à vérifier — le prix du prospectus y est bien le prix qu'on paiera.
        """
        if not self.available:
            return False
        return bool(self.verified_in_drive or not self.requires_drive_verification)

    @property
    def best_unit_price(self) -> float | None:
        """Le prix sur lequel on compare : l'effectif s'il existe."""
        return self.effective_unit_price if self.effective_unit_price is not None else self.unit_price

    def pack_label(self) -> str:
        if self.pack_size is None or self.pack_unit is None:
            return "format non précisé"
        return units.Pack(self.pack_size, self.pack_unit, self.pack_count).describe()

    def to_row(self) -> dict[str, Any]:
        row = dict(self.__dict__)
        row["observed_at"] = self.observed_at.isoformat()
        for key in ("loyalty_valid_until", "valid_from", "valid_until"):
            value = row.get(key)
            row[key] = value.isoformat() if value else None
        return row


# --------------------------------------------------------------------------- #
# Verdicts de validation
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    OK = "ok"        # utilisable
    FLAG = "flag"    # utilisable avec réserve explicite, jamais chiffré au kilo
    REJECT = "reject"  # jeté : donnée fausse ou produit non conforme


@dataclass
class Verdict:
    status: Status
    rules: list[str] = field(default_factory=list)   # 'P1', 'P5', 'C1'…
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is Status.OK

    @property
    def rejected(self) -> bool:
        return self.status is Status.REJECT

    def merge(self, other: "Verdict") -> "Verdict":
        """Le plus sévère gagne ; les motifs s'accumulent."""
        order = {Status.OK: 0, Status.FLAG: 1, Status.REJECT: 2}
        status = self.status if order[self.status] >= order[other.status] else other.status
        return Verdict(status, self.rules + other.rules, self.reasons + other.reasons)

    def explain(self) -> str:
        if not self.reasons:
            return "conforme"
        return "; ".join(
            f"[{rule}] {reason}" for rule, reason in zip(self.rules, self.reasons)
        )


def OK(rule: str = "", reason: str = "") -> Verdict:
    return Verdict(Status.OK, [rule] if rule else [], [reason] if reason else [])


def FLAG(reason: str, rule: str = "") -> Verdict:
    return Verdict(Status.FLAG, [rule or "?"], [reason])


def REJECT(reason: str, rule: str = "") -> Verdict:
    return Verdict(Status.REJECT, [rule or "?"], [reason])


# --------------------------------------------------------------------------- #
# Sortie
# --------------------------------------------------------------------------- #
class Grade(str, Enum):
    STOCK = "stock"      # sous le seuil de stockage → on achète en gros
    GOOD = "good"        # bonne affaire
    NORMAL = "normal"    # prix normal, on prend si c'est sur la liste
    TOO_HIGH = "too_high"  # au-dessus du plafond → on attend


@dataclass
class Offer:
    """Une observation validée, notée, prête à être affectée à un magasin."""

    observation: PriceObservation
    item: BasketItem
    verdict: Verdict
    grade: Grade
    saving_eur: float = 0.0        # économie estimée sur la quantité du run
    is_record: bool = False
    previous_best: float | None = None
    comment: str = ""

    @property
    def store_id(self) -> str:
        return self.observation.store_id

    @property
    def unit_price(self) -> float | None:
        return self.observation.best_unit_price

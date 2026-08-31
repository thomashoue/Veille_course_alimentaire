"""Socle des clients drive : produits, panier, idempotence."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..units import Pack, parse_pack, strip_accents

log = logging.getLogger(__name__)


class DriveError(RuntimeError):
    """Le drive n'a pas répondu comme attendu."""


class DriveUnavailable(DriveError):
    """Playwright absent, session non ouverte, site injoignable."""


@dataclass
class DriveProduct:
    ref: str
    label: str
    price_eur: float | None = None
    pack: Pack | None = None
    url: str | None = None
    available: bool = True
    # Prix unitaire affiché par l'enseigne elle-même (« 0,92 € / l »). On ne
    # s'en sert JAMAIS comme prix de référence — le nôtre reste calculé — mais
    # il permet de retrouver un format absent du libellé, et de recouper.
    unit_price_hint: float | None = None
    unit_hint_unit: str | None = None
    # Prix barré affiché à côté (« au lieu de ») : alimente regular_price.
    regular_price: float | None = None
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.pack is None:
            self.pack = parse_pack(self.label)


@dataclass
class CartLine:
    ref: str
    label: str
    quantity: int
    price_eur: float | None = None


# --------------------------------------------------------------------------- #
# Rapprochement libellé agrégateur ↔ libellé drive
# --------------------------------------------------------------------------- #
_STOPWORDS = {"de", "du", "des", "le", "la", "les", "au", "aux", "en", "a", "l", "d", "et"}


def tokens(label: str) -> set[str]:
    plain = strip_accents(label or "").lower()
    return {t for t in re.split(r"[^a-z0-9]+", plain) if t and t not in _STOPWORDS and len(t) > 1}


def match_score(candidate: str, reference: str) -> float:
    """Similarité 0..1 entre deux libellés, avec bonus si le format concorde.

    Le format compte double : « thon 3x93 g » et « thon 140 g » ne sont pas le
    même produit, même si les mots se ressemblent.
    """
    a, b = tokens(candidate), tokens(reference)
    if not a or not b:
        return 0.0
    jaccard = len(a & b) / len(a | b)
    pack_a, pack_b = parse_pack(candidate), parse_pack(reference)
    if pack_a and pack_b:
        try:
            same = abs(pack_a.total_base - pack_b.total_base) < 1e-6
        except Exception:
            same = False
        jaccard = jaccard * 0.7 + (0.3 if same else 0.0)
    return jaccard


def best_match(products: list[DriveProduct], reference: str, threshold: float = 0.34):
    """Meilleur produit du drive pour un libellé donné, ou None si trop lointain."""
    scored = [(match_score(p.label, reference), p) for p in products]
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] >= threshold:
        return scored[0][1]
    return None


# --------------------------------------------------------------------------- #
class DriveClient:
    """Interface commune. Toute mutation du panier est vérifiée, jamais supposée.

    Le §7 est formel : les clics échouent silencieusement. On ne fait donc
    jamais confiance au retour d'une action — on relit l'état du panier.
    """

    banner = "?"
    max_attempts = 3

    def __init__(self, store, dry_run: bool = False):
        self.store = store
        self.dry_run = dry_run

    # -- à implémenter par enseigne ------------------------------------- #
    def search(self, query: str, limit: int = 12) -> list[DriveProduct]:
        raise NotImplementedError

    def cart_state(self) -> list[CartLine]:
        raise NotImplementedError

    def _add(self, product: DriveProduct, quantity: int) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- logique commune ------------------------------------------------ #
    def cart_add(self, product: DriveProduct, quantity: int = 1) -> CartLine:
        """Ajoute au panier de façon idempotente.

        On relit le panier avant et après : si la ligne est déjà à la bonne
        quantité, on ne fait rien ; si l'ajout n'a rien changé, on réessaie.
        """
        if self.dry_run:
            log.info("[dry-run] %s : ajouter %sx %s", self.banner, quantity, product.label)
            return CartLine(product.ref, product.label, quantity, product.price_eur)

        for attempt in range(1, self.max_attempts + 1):
            before = {line.ref: line for line in self.cart_state()}
            current = before.get(product.ref)
            if current and current.quantity >= quantity:
                return current

            missing = quantity - (current.quantity if current else 0)
            self._add(product, missing)

            after = {line.ref: line for line in self.cart_state()}
            line = after.get(product.ref)
            if line and line.quantity >= quantity:
                return line
            log.warning(
                "%s : ajout non confirmé pour %s (tentative %s/%s)",
                self.banner,
                product.label,
                attempt,
                self.max_attempts,
            )

        raise DriveError(f"impossible de confirmer l'ajout de {product.label} au panier")

    def fill(self, wanted: list[tuple[DriveProduct, int]]) -> list[CartLine]:
        """Remplit le panier et s'arrête là. Créneau et paiement restent humains."""
        lines: list[CartLine] = []
        for product, quantity in wanted:
            try:
                lines.append(self.cart_add(product, quantity))
            except DriveError as exc:
                log.error("%s : %s", self.banner, exc)
        return lines


# --------------------------------------------------------------------------- #
class FixtureDriveClient(DriveClient):
    """Client hors ligne alimenté par un dictionnaire de produits.

    Sert aux tests et au mode ``--offline`` : tout le pipeline peut tourner
    sans réseau, ce qui est la condition pour que ``validate`` reste testable.
    """

    banner = "fixture"

    def __init__(self, store, catalogue: dict[str, list[DriveProduct]] | None = None, **kwargs):
        super().__init__(store, **kwargs)
        self.catalogue = catalogue or {}
        self._cart: dict[str, CartLine] = {}

    def search(self, query: str, limit: int = 12) -> list[DriveProduct]:
        needle = strip_accents(query).lower()
        results: list[DriveProduct] = []
        for key, products in self.catalogue.items():
            if strip_accents(key).lower() in needle or needle in strip_accents(key).lower():
                results += products
        if not results:
            results = [
                p
                for products in self.catalogue.values()
                for p in products
                if match_score(p.label, query) > 0.3
            ]
        return results[:limit]

    def cart_state(self) -> list[CartLine]:
        return list(self._cart.values())

    def _add(self, product: DriveProduct, quantity: int) -> None:
        line = self._cart.get(product.ref)
        if line:
            line.quantity += quantity
        else:
            self._cart[product.ref] = CartLine(
                product.ref, product.label, quantity, product.price_eur
            )

"""Normalisation des unités et lecture des conditionnements.

Constat C3 : le prix affiché n'est presque jamais comparable d'une enseigne à
l'autre. Tout passe donc par une unité de base unique par famille :

    masse    → kg
    volume   → L
    longueur → m
    compte   → unité (rouleau, dose, lavage, capsule… valent 1)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MASS = {"mg": 1e-6, "g": 1e-3, "gr": 1e-3, "kg": 1.0}
VOLUME = {"ml": 1e-3, "cl": 1e-2, "dl": 1e-1, "l": 1.0}
LENGTH = {"cm": 1e-2, "m": 1.0}
COUNT = {
    "unite": 1.0,
    "piece": 1.0,
    "rouleau": 1.0,
    "dose": 1.0,
    "lavage": 1.0,
    "capsule": 1.0,
    "tablette": 1.0,
    "sachet": 1.0,
    "boite": 1.0,
    "brique": 1.0,
    "bouteille": 1.0,
}

BASE_UNIT = {"mass": "kg", "volume": "L", "length": "m", "count": "unite"}

# Libellés rencontrés dans les drives → unité canonique.
_ALIASES = {
    "grammes": "g",
    "gramme": "g",
    "kilo": "kg",
    "kilos": "kg",
    "kilogramme": "kg",
    "litre": "l",
    "litres": "l",
    "lt": "l",
    "millilitre": "ml",
    "centilitre": "cl",
    "rouleaux": "rouleau",
    "rlx": "rouleau",
    "rl": "rouleau",
    "doses": "dose",
    "lavages": "lavage",
    "capsules": "capsule",
    "tablettes": "tablette",
    "sachets": "sachet",
    "pieces": "piece",
    "pces": "piece",
    "pce": "piece",
    "unites": "unite",
    "unité": "unite",
    "unités": "unite",
    "u": "unite",
    "metre": "m",
    "metres": "m",
    "mètre": "m",
    "mètres": "m",
}


class UnknownUnit(ValueError):
    """Unité non reconnue — on refuse de deviner plutôt que de calculer faux."""


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def canonical_unit(unit: str) -> str:
    """'Litres' → 'l', 'RLX' → 'rouleau'. Lève UnknownUnit si inconnue."""
    if unit is None:
        raise UnknownUnit("unité absente")
    key = strip_accents(str(unit).strip().lower()).rstrip(".")
    key = _ALIASES.get(key, key)
    if key in MASS or key in VOLUME or key in LENGTH or key in COUNT:
        return key
    raise UnknownUnit(f"unité inconnue : {unit!r}")


def family(unit: str) -> str:
    u = canonical_unit(unit)
    if u in MASS:
        return "mass"
    if u in VOLUME:
        return "volume"
    if u in LENGTH:
        return "length"
    return "count"


def base_unit(unit: str) -> str:
    """Unité de comparaison de la famille : 'g' → 'kg', 'cl' → 'L'."""
    return BASE_UNIT[family(unit)]


def to_base(size: float, unit: str) -> float:
    """Convertit une quantité vers l'unité de base de sa famille."""
    u = canonical_unit(unit)
    for table in (MASS, VOLUME, LENGTH, COUNT):
        if u in table:
            return float(size) * table[u]
    raise UnknownUnit(unit)  # pragma: no cover - couvert par canonical_unit


def same_family(unit_a: str, unit_b: str) -> bool:
    try:
        return family(unit_a) == family(unit_b)
    except UnknownUnit:
        return False


# Une dose, un lavage, une capsule et une tablette de lessive désignent la même
# chose. Un rouleau n'est pas une dose : dans la famille « compte », on ne
# compare que ce qui est réellement interchangeable.
DOSE_SYNONYMS = {"dose", "lavage", "capsule", "tablette"}


def comparable_units(unit_a: str, unit_b: str) -> bool:
    """Deux unités sont comparables si un seuil exprimé dans l'une a un sens
    pour un prix exprimé dans l'autre."""
    if not same_family(unit_a, unit_b):
        return False
    if family(unit_a) != "count":
        return True
    a, b = canonical_unit(unit_a), canonical_unit(unit_b)
    if a == b:
        return True
    return {a, b} <= DOSE_SYNONYMS


@dataclass(frozen=True)
class Pack:
    """Conditionnement lu dans un libellé : 6 x 1 L → count=6, size=1, unit='l'."""

    size: float
    unit: str
    count: int = 1

    @property
    def total_base(self) -> float:
        """Quantité totale du pack, dans l'unité de base."""
        return to_base(self.size * self.count, self.unit)

    def describe(self) -> str:
        size = f"{self.size:g}"
        if self.count > 1:
            return f"{self.count}x{size} {self.unit}"
        return f"{size} {self.unit}"


_UNIT_RE = (
    r"(kg|kilos?|kilogrammes?|g|gr|grammes?|mg"
    r"|l|lt|litres?|cl|centilitres?|ml|millilitres?|dl"
    r"|rouleaux?|rlx|rl|doses?|lavages?|capsules?|tablettes?|sachets?"
    r"|pi[eè]ces?|pces?|unit[eé]s?|m[eè]tres?|m|cm)"
)
_NUM = r"(\d+(?:[.,]\d+)?)"

# « 6x1L », « 2 x 500 g », « lot de 3 x 75 cl »
_MULTI_RE = re.compile(rf"(\d+)\s*[x×*]\s*{_NUM}\s*{_UNIT_RE}\b", re.I)
# « 5 L », « 640g », « 24 rouleaux »
_SIMPLE_RE = re.compile(rf"{_NUM}\s*{_UNIT_RE}\b", re.I)


def _to_float(raw: str) -> float:
    return float(raw.replace(",", "."))


def parse_pack(label: str) -> Pack | None:
    """Extrait le conditionnement d'un libellé de drive.

    Renvoie None si le libellé ne dit rien du format : c'est le cas P4 de la
    spec, où il est interdit de calculer un prix au kilo.
    """
    if not label:
        return None
    text = strip_accents(label)

    multi = _MULTI_RE.search(text)
    if multi:
        count, size, unit = multi.groups()
        try:
            return Pack(_to_float(size), canonical_unit(unit), int(count))
        except UnknownUnit:
            return None

    # On garde la plus grande quantité mentionnée : « thon 3x140g (net 3x93g) »
    # doit se lire sur le format annoncé, pas sur un chiffre parasite.
    best: Pack | None = None
    for match in _SIMPLE_RE.finditer(text):
        size, unit = match.groups()
        try:
            pack = Pack(_to_float(size), canonical_unit(unit))
        except UnknownUnit:
            continue
        if best is None or pack.total_base > best.total_base:
            best = pack
    return best


def format_price(value: float, unit: str) -> str:
    """« 0,94 €/L » — format français, celui du rapport.

    Sous 1 €, on passe à trois décimales : sur la litière ou la lessive, le
    troisième chiffre est précisément celui qui départage deux offres.
    """
    digits = 3 if 0 < value < 1 else 2
    return f"{value:.{digits}f}".replace(".", ",") + f" €/{unit}"


def format_eur(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + " €"

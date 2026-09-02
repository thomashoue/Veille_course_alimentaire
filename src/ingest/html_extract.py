"""Extraction HTML sans dépendance externe (html.parser de la stdlib).

Deux stratégies, dans cet ordre :
  1. JSON-LD (``<script type="application/ld+json">``) — de loin le plus stable
     quand le site en publie : c'est du Product/Offer normalisé ;
  2. à défaut, découpe en blocs par balise + classe, puis lecture par motifs.

Les agrégateurs changent de gabarit régulièrement. Quand plus rien n'est
extrait, le collecteur doit le dire bruyamment plutôt que renvoyer une liste
vide silencieuse — un rapport vide qui ressemble à « pas de promo » est pire
qu'une erreur.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from ..units import strip_accents


# --------------------------------------------------------------------------- #
# Blocs
# --------------------------------------------------------------------------- #
@dataclass
class Block:
    tag: str
    attrs: dict[str, str]
    text: str = ""
    html: str = ""
    links: list[str] = field(default_factory=list)


class _BlockParser(HTMLParser):
    """Collecte les éléments dont l'attribut ``class`` contient un motif."""

    def __init__(self, tag: str, class_contains: str | None):
        super().__init__(convert_charrefs=True)
        self.wanted_tag = tag
        self.needle = (class_contains or "").lower()
        self.blocks: list[Block] = []
        self._stack: list[Block] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {k: (v or "") for k, v in attrs}
        if self._stack:
            self._depth += 1 if tag == self.wanted_tag else 0
            if tag == "a" and attributes.get("href"):
                self._stack[-1].links.append(attributes["href"])
            return
        if tag != self.wanted_tag:
            return
        classes = attributes.get("class", "").lower()
        identifier = attributes.get("id", "").lower()
        if not self.needle or self.needle in classes or self.needle in identifier:
            self._stack.append(Block(tag, attributes))
            self._depth = 0

    def handle_endtag(self, tag: str) -> None:
        if not self._stack or tag != self.wanted_tag:
            return
        if self._depth:
            self._depth -= 1
            return
        block = self._stack.pop()
        block.text = re.sub(r"\s+", " ", block.text).strip()
        self.blocks.append(block)

    def handle_data(self, data: str) -> None:
        if self._stack:
            self._stack[-1].text += " " + data


def find_blocks(html: str, tag: str = "div", class_contains: str | None = None) -> list[Block]:
    parser = _BlockParser(tag, class_contains)
    try:
        parser.feed(html or "")
    except Exception:  # un HTML cassé ne doit pas tuer le run
        pass
    return parser.blocks


class _TextParser(HTMLParser):
    SKIP = {"script", "style", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def to_text(html: str) -> str:
    parser = _TextParser()
    try:
        parser.feed(html or "")
    except Exception:
        pass
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def extract_jsonld(html: str) -> list[dict]:
    """Renvoie tous les objets JSON-LD de la page, aplatis."""
    found: list[dict] = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html or "",
        re.S | re.I,
    ):
        try:
            data = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                found.append(node)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return found


# --------------------------------------------------------------------------- #
# Motifs de prix et de mécaniques
# --------------------------------------------------------------------------- #
_PRICE_RE = re.compile(
    r"(?<![\d,.])(\d{1,4})[\s.,](\d{2})\s*€|"      # 2,51 €  /  2.51 €
    # « 5 € ,52 » — Leclerc Drive découpe l'euro et les centimes en deux
    # éléments distincts. Sans cette alternative on lit 5,00 € au lieu de
    # 5,52 € : une erreur de 52 centimes, systématique et silencieuse.
    r"(?<![\d,.])(\d{1,4})\s*€\s*,?\s*(\d{2})\b|"
    r"€\s*(\d{1,4})[.,](\d{2})|"                   # € 2,51
    r"(?<![\d,.])(\d{1,4})\s*€(?![\d])"            # 4 €
)

# « 0,92 € / l », « 3,07 € le kg » — le prix unitaire que l'enseigne affiche
# elle-même. Précieux : il permet de retrouver le format quand le libellé ne
# le donne pas, et de recouper notre propre calcul.
_UNIT_PRICE_RE = re.compile(
    r"(\d{1,4})[.,](\d{1,3})\s*€\s*(?:/|le|par|au)\s*"
    r"(kg|kilos?|g|l|litres?|cl|ml|m|pi[eè]ce|unit[eé]|rouleau|dose|lavage)\b",
    re.I,
)


def parse_pack_price(text: str) -> float | None:
    """Prix du PACK : premier prix qui n'est pas un €/kg ni un €/portion.

    Intermarché affiche « la boîte de 87g net égoutté • 28,05 €/Kg » et parfois
    « 5,82 €/pers » (suggestion recette). Aucun n'est le prix de la boîte : on
    les masque avant de lire.
    """
    if not text:
        return None
    masked = _UNIT_PRICE_RE.sub(" ", text)
    masked = re.sub(r"\d+[.,]\d{1,2}\s*€\s*/\s*\w+", " ", masked)   # €/pers, €/Kg
    return parse_price(masked)


def parse_unit_price(text: str) -> tuple[float, str] | None:
    """Lit le prix unitaire affiché par l'enseigne, s'il y en a un."""
    match = _UNIT_PRICE_RE.search(text or "")
    if not match:
        return None
    value = float(f"{match.group(1)}.{match.group(2)}")
    return value, match.group(3).lower()


def parse_price(text: str) -> float | None:
    """Premier prix lisible d'un texte, en euros."""
    if not text:
        return None
    match = _PRICE_RE.search(text)
    if not match:
        return None
    groups = match.groups()
    for i in range(0, 6, 2):
        if groups[i] is not None:
            return float(f"{groups[i]}.{groups[i + 1]}")
    return float(groups[6])


def parse_all_prices(text: str) -> list[float]:
    prices: list[float] = []
    for match in _PRICE_RE.finditer(text or ""):
        groups = match.groups()
        value = None
        for i in range(0, 6, 2):
            if groups[i] is not None:
                value = float(f"{groups[i]}.{groups[i + 1]}")
                break
        if value is None and groups[6] is not None:
            value = float(groups[6])
        if value is not None:
            prices.append(value)
    return prices


_MECHANIC_PATTERNS = [
    # « -30 % sur le 2ᵉ », « le 2ème à -50% », « 2e à moitié prix »
    (re.compile(r"-?\s*(\d{2})\s*%[^.]{0,30}(2\s*(?:e|eme|ème|nd))", re.I), "second"),
    (re.compile(r"(2\s*(?:e|eme|ème|nd))[^.]{0,30}-?\s*(\d{2})\s*%", re.I), "second_rev"),
    (re.compile(r"2\s*(?:e|eme|ème)\s*(?:a|à)\s*moiti[eé]\s*prix", re.I), "second_50"),
    (re.compile(r"\b3\s*(?:pour|=)\s*2\b", re.I), "3_pour_2"),
    (re.compile(r"\b2\s*\+\s*1\s*(?:gratuit|offert)", re.I), "3_pour_2"),
    (re.compile(r"\b1\s*\+\s*1\s*(?:gratuit|offert)", re.I), "second_-100"),
    (re.compile(r"\blot\s*de\s*\d+", re.I), "lot"),
]


def detect_mechanic(text: str) -> str | None:
    """Reconnaît la mécanique promotionnelle annoncée.

    C'est le piège numéro un des agrégateurs (C2) : le prix du 2ᵉ article
    présenté comme le prix promo.
    """
    if not text:
        return None
    plain = strip_accents(text)
    for pattern, kind in _MECHANIC_PATTERNS:
        match = pattern.search(plain)
        if not match:
            continue
        if kind == "second":
            return f"second_-{int(match.group(1))}"
        if kind == "second_rev":
            return f"second_-{int(match.group(2))}"
        if kind == "second_50":
            return "second_-50"
        return kind
    return None


def detect_weight_basis(text: str) -> str | None:
    """'net_egoutte' | 'brut' | None.

    On ne devine pas : sans mention explicite on renvoie None, ce qui déclenche
    le FLAG P5 au lieu d'un €/kg inventé.
    """
    if not text:
        return None
    plain = strip_accents(text).lower()
    if "egoutte" in plain:
        return "net_egoutte"
    if "poids net" in plain or "poids brut" in plain:
        return "brut"
    return None


_LOYALTY_RE = re.compile(r"(\d{1,2})\s*%[^.]{0,25}(carte|fid[ée]lit|cagnott)", re.I)


def detect_loyalty_pct(text: str) -> float | None:
    match = _LOYALTY_RE.search(strip_accents(text or ""))
    return float(match.group(1)) if match else None

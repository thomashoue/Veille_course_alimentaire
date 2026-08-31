"""Lecture d'une page de drive ENREGISTRÉE depuis le navigateur habituel.

Pourquoi ce module existe : Leclerc Drive détecte et bloque les navigateurs
pilotés (« Accès temporairement restreint — quelque chose dans le comportement
du navigateur nous a intrigué »). Constaté le 2026-08-31.

La réponse n'est pas de déguiser l'automate, mais de retirer l'automate du
chemin : l'humain navigue normalement, enregistre la page (Ctrl+S), et le code
lit le fichier. Aucun pilotage, aucune détection possible, et — c'est le point
important — l'invariant C1 est préservé : ce qu'on lit vient bien du drive,
donc ``verified_in_drive=True`` est mérité.

C'est aussi la voie la plus durable : un fichier HTML enregistré ne change pas
de comportement selon l'humeur d'un pare-feu applicatif.
"""

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser

from ..config import Config
from ..ingest.html_extract import (
    extract_jsonld,
    parse_price,
    parse_unit_price,
    to_text,
)
from ..models import PriceObservation, Source
from ..units import parse_pack, strip_accents
from .base import DriveProduct

log = logging.getLogger(__name__)

# Un bloc produit crédible : assez de texte pour porter un libellé, pas assez
# pour être une page entière.
MIN_BLOCK_TEXT = 8
MAX_BLOCK_TEXT = 300

# Gabarits de vignettes rencontrés selon les enseignes. On essaie tout : c'est
# une lecture de fichier, une passe de plus ne coûte rien.
# `_Product` couvre Leclerc Drive (`li.liWCRS310_Product`, relevé 2026-08-31).
_BLOCK_PATTERNS = [
    ("li", "_product"),
    ("li", "produit"),
    ("li", "product"),
    ("div", "produit"),
    ("div", "product"),
    ("article", "product"),
    ("article", None),
    ("li", None),
]

# Sous-éléments à lire en priorité dans une vignette, par fragment de classe.
# Relevé Courses U (SFCC) du 2026-08-31 : le prix barré (price-standard) est
# rendu AVANT le prix réel (price-sales) — lire « le premier prix du texte »
# prendrait le barré pour le vrai sur chaque promo.
_PRICE_CHILD = ("price-sales", "prix-vente")
_REGULAR_CHILD = ("price-standard", "prix-barre", "old-price", "was-price")
_UNIT_CHILD = ("unit-info", "prixunitemesure", "prix-par-unite", "price-unit")


def _child_text(tile: dict, needles: tuple[str, ...]) -> str | None:
    for child in tile.get("children", []):
        cls = child.get("class", "")
        if any(needle in cls for needle in needles):
            text = child.get("full_text", "")
            if text:
                return text
    return None


# Vignettes sponsorisées : ce sont des publicités, pas l'assortiment. Elles
# portent un prix et passeraient sinon pour des offres.
_SPONSORED_CLASSES = ("mkp", "trade", "rmp", "sponsor", "publi")
_SPONSORED_TEXT = ("sponsorise", "publicite")

# Bruit d'interface présent dans le texte d'une vignette : sélecteur de
# quantité, bouton d'ajout, lien « Voir le produit ».
_LABEL_CUTS = re.compile(
    r"(?i)\b(ajouter au panier|ajouter|voir le produit|voir le detail|en stock)\b"
)

# Balises qui ne se ferment jamais : elles ne doivent pas encombrer la pile.
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class _TileCollector(HTMLParser):
    """Découpe une page en vignettes, en survivant au HTML mal formé.

    Les pages de drive réelles ne ferment pas toutes leurs balises. Un lecteur
    qui suppose un HTML propre décroche au premier `<li>` orphelin et ne
    ramène plus rien — c'est ce qui est arrivé sur la page Leclerc de 822 Ko,
    où 13 vignettes étaient bien présentes et 2 seulement ressortaient.

    À la fermeture d'une balise, on cherche son ouverture dans la pile et on
    jette ce qui traîne au-dessus, au lieu d'abandonner.
    """

    def __init__(self, tag: str | None, class_contains: str | None):
        super().__init__(convert_charrefs=True)
        self.wanted_tag = tag          # None = tout collecter (diagnostic)
        self.needle = (class_contains or "").lower()
        self.tiles: list[dict] = []
        self._stack: list[dict] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in ("script", "style", "noscript"):
            self._skip += 1
            return
        if self._skip or tag in _VOID_TAGS:
            return
        attributes = {k: (v or "") for k, v in attrs}
        self._stack.append(
            {
                "tag": tag,
                "attrs": attributes,
                "class": (attributes.get("class") or attributes.get("id") or "").lower(),
                "text": [],
                "children": [],
            }
        )

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip or tag in _VOID_TAGS:
            return
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index]["tag"] != tag:
                continue
            node = self._stack[index]
            # Les balises restées ouvertes à l'intérieur portent le texte utile :
            # on le rapatrie au lieu de le jeter avec elles. Sans ça, une
            # vignette dont le <div> n'est pas fermé ressort vide.
            for orphan in self._stack[index + 1 :]:
                node["text"].append(" ".join(orphan["text"]))
                node["children"].append(orphan)
            del self._stack[index:]
            break
        else:
            return

        text = re.sub(r"\s+", " ", " ".join(node["text"])).strip()
        node["full_text"] = text
        if self._stack:
            parent = self._stack[-1]
            parent["text"].append(text)
            parent["children"].append(node)
            parent["children"].extend(node["children"])

        if (self.wanted_tag is None or node["tag"] == self.wanted_tag) and (
            not self.needle or self.needle in node["class"]
        ):
            self.tiles.append(node)

    def handle_data(self, data):  # noqa: ANN001
        if not self._skip and self._stack:
            self._stack[-1]["text"].append(data)


def iter_tiles(html: str, tag: str, class_contains: str | None) -> list[dict]:
    collector = _TileCollector(tag, class_contains)
    try:
        collector.feed(html or "")
    except Exception as exc:
        log.debug("lecture interrompue : %s", exc)
    return collector.tiles


def _is_sponsored(tile: dict) -> bool:
    if any(needle in tile["class"] for needle in _SPONSORED_CLASSES):
        return True
    plain = strip_accents(tile.get("full_text", "")).lower()
    return any(needle in plain for needle in _SPONSORED_TEXT)


def _clean_label(text: str) -> str:
    """Isole le libellé produit du bruit d'interface de la vignette.

    Une vignette Leclerc donne : « Lait demi-écrémé Eco+ Ajouter au panier
    - 0 + 5 € ,52 0,92 € / l Voir ». Le libellé s'arrête au premier bouton.
    """
    text = re.sub(r"\s+", " ", text).strip()
    cut = _LABEL_CUTS.search(text)
    if cut and cut.start() >= 5:
        text = text[: cut.start()]
    # D'abord les prix unitaires (« 0,95 €/l », « 3,07 € le kg ») : les retirer
    # en entier évite de laisser traîner un « /l » orphelin dans le libellé.
    text = re.sub(r"\d+[.,]\d+\s*€\s*(?:/|le|par|au)\s*[a-zéè]{1,10}\b", " ", text, flags=re.I)
    # Reste des prix et du sélecteur de quantité pour les gabarits sans bouton.
    text = re.sub(r"\d+[.,]\d{2}\s*€|\d+\s*€\s*,?\s*\d{2}|€\s*\d+[.,]\d{2}|\d+\s*€", " ", text)
    text = re.sub(r"(?i)\b(le kg|le l|par kg|prix au kilo|sponsoris\w*)\b", " ", text)
    text = re.sub(r"(?<= )[-+](?= )|\s0\s\+", " ", text)
    # On ne rogne pas le « + » collé : « Eco+ » est une marque, pas un séparateur.
    text = re.sub(r"\s+", " ", text).strip(" -·|")
    # Chez U, l'image et le lien portent le même texte : « Nom sac 5L Nom sac
    # 5L ». On replie le doublon exact.
    words = text.split()
    half = len(words) // 2
    if half >= 2 and words[:half] == words[half:]:
        text = " ".join(words[:half])
    return text


def products_from_jsonld(html: str) -> list[DriveProduct]:
    products: list[DriveProduct] = []
    for node in extract_jsonld(html):
        if str(node.get("@type", "")).lower() != "product":
            continue
        label = str(node.get("name") or "").strip()
        if not label:
            continue
        offers = node.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = (offers or {}).get("price") if isinstance(offers, dict) else None
        try:
            price = float(str(price).replace(",", ".")) if price is not None else None
        except ValueError:
            price = None
        availability = str((offers or {}).get("availability", "")).lower()
        products.append(
            DriveProduct(
                ref=str(node.get("sku") or node.get("productID") or label)[:80],
                label=label,
                price_eur=price,
                available="outofstock" not in availability,
            )
        )
    return products


def products_from_embedded_json(html: str) -> list[DriveProduct]:
    """Beaucoup de drives déposent leur catalogue dans un JSON inline.

    C'est la même donnée que celle du XHR, déjà présente dans la page : la voie
    la plus fiable après le JSON-LD.
    """
    products: list[DriveProduct] = []
    for match in re.finditer(
        r"(?:window\.__[A-Z_]+__|__NEXT_DATA__|__NUXT__|dataLayer)\s*=\s*(\{.*?\})\s*[;<]",
        html,
        re.S,
    ):
        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            label = node.get("libelle") or node.get("label") or node.get("name")
            price = node.get("prix") or node.get("price") or node.get("prixUnitaire")
            if not label or price is None or not isinstance(label, str):
                continue
            try:
                price = float(str(price).replace(",", ".").replace(" ", ""))
            except ValueError:
                continue
            if price <= 0 or price > 500:
                continue
            products.append(
                DriveProduct(
                    ref=str(node.get("ref") or node.get("id") or label)[:80],
                    label=label.strip(),
                    price_eur=price,
                    available=bool(node.get("disponible", node.get("available", True))),
                )
            )
    return products


def products_from_blocks(html: str) -> list[DriveProduct]:
    """Dernier recours : repérer les vignettes qui portent un prix ET un libellé."""
    for tag, needle in _BLOCK_PATTERNS:
        found: list[DriveProduct] = []
        for tile in iter_tiles(html, tag, needle):
            text = tile.get("full_text", "")
            if not (MIN_BLOCK_TEXT <= len(text) <= MAX_BLOCK_TEXT):
                continue
            if _is_sponsored(tile):
                continue
            # Les sous-éléments typés d'abord, le texte brut en filet.
            price_text = _child_text(tile, _PRICE_CHILD)
            price = parse_price(price_text) if price_text else parse_price(text)
            regular_text = _child_text(tile, _REGULAR_CHILD)
            regular = parse_price(regular_text) if regular_text else None
            label = _clean_label(text)
            if price is None or len(label) < 5:
                continue
            unit_hint = parse_unit_price(_child_text(tile, _UNIT_CHILD) or text)
            found.append(
                DriveProduct(
                    ref=(tile["attrs"].get("data-ref")
                         or tile["attrs"].get("data-itemid")
                         or tile["attrs"].get("id") or label)[:80],
                    label=label[:140],
                    price_eur=price,
                    regular_price=regular,
                    available="indisponible" not in strip_accents(text).lower(),
                    unit_price_hint=unit_hint[0] if unit_hint else None,
                    unit_hint_unit=unit_hint[1] if unit_hint else None,
                )
            )
        if found:
            return found
    return []


def extract_products(html: str) -> tuple[list[DriveProduct], str]:
    """Extrait les produits d'une page enregistrée. Renvoie ``(produits, méthode)``."""
    for method, extractor in (
        ("json-ld", products_from_jsonld),
        ("json embarqué", products_from_embedded_json),
        ("blocs HTML", products_from_blocks),
    ):
        products = extractor(html)
        # On dédoublonne : un même produit apparaît souvent deux fois (vignette
        # + version mobile masquée).
        unique: dict[str, DriveProduct] = {}
        for product in products:
            key = (product.label.lower(), product.price_eur)
            unique.setdefault(str(key), product)
        if unique:
            return list(unique.values()), method
    return [], "aucune"


def observations_from_page(
    html: str,
    store,
    config: Config,
    *,
    source_url: str | None = None,
) -> tuple[list[PriceObservation], dict]:
    """Transforme une page enregistrée en observations vérifiées en drive.

    Seuls les produits rattachables à un article du panier sont retenus : une
    page de résultats contient des dizaines de références dont on n'a que faire.
    """
    products, method = extract_products(html)
    observations: list[PriceObservation] = []
    unmatched = 0

    derived = 0
    for product in products:
        item = config.match_item(product.label)
        if item is None:
            unmatched += 1
            continue
        if product.price_eur is None:
            continue
        pack = product.pack or parse_pack(product.label)
        extra_notes: list[str] = []

        hint = _pack_from_unit_price(product)
        if pack is None and hint is not None:
            # Le libellé ne dit pas le format, mais l'enseigne affiche son prix
            # au litre : 5,52 € ÷ 0,92 €/L = 6 L. On récupère ainsi un P4 qui
            # serait resté « format non précisé ».
            pack = hint
            derived += 1
            extra_notes.append(
                f"format {pack.describe()} déduit du prix unitaire affiché "
                f"({product.unit_price_hint:g} €/{product.unit_hint_unit})"
            )
        suspect_reason = None
        if pack is not None and pack is not hint and product.unit_price_hint:
            note, suspect_reason = _cross_check(product, pack)
            if note:
                extra_notes.append(note)
        observations.append(
            PriceObservation(
                store_id=store.id,
                basket_item_id=item.id,
                product_label=product.label,
                price_eur=product.price_eur,
                category=item.category,
                pack_size=pack.size if pack else None,
                pack_unit=pack.unit if pack else None,
                pack_count=pack.count if pack else 1,
                regular_price=product.regular_price,
                source=Source.DRIVE.value,
                # Mérité : cette page vient bien du drive, chargée par l'humain.
                verified_in_drive=True,
                available=product.available,
                source_url=source_url,
                banner=store.banner,
                drive_ref=product.ref,
                notes=[f"lu dans une page enregistrée ({method})", *extra_notes],
                suspect_reason=suspect_reason,
            )
        )

    report = {
        "method": method,
        "products_found": len(products),
        "matched_to_basket": len(observations),
        "ignored_not_in_basket": unmatched,
        "packs_derived_from_unit_price": derived,
        "page_size": len(html),
        "page_text_size": len(to_text(html)),
    }
    return observations, report


# --------------------------------------------------------------------------- #
# Diagnostic de gabarit
# --------------------------------------------------------------------------- #
def analyze_page(html: str, top: int = 12) -> dict:
    """Découvre le gabarit d'une page inconnue.

    Ne renvoie que de la structure et des extraits courts, pour pouvoir être
    recopié dans une conversation sans exposer un compte.
    """
    # Même lecteur que l'extraction : un seul parseur à maintenir, et le
    # diagnostic voit donc exactement ce que verra `parse-page`.
    counts: dict[tuple[str, str], int] = {}
    samples: dict[tuple[str, str], str] = {}
    for node in iter_tiles(html, None, None):
        text = node.get("full_text", "")
        if not (MIN_BLOCK_TEXT <= len(text) <= 400):
            continue
        raw_class = node["attrs"].get("class") or node["attrs"].get("id") or ""
        if not raw_class or parse_price(text) is None:
            continue
        key = (node["tag"], raw_class.split()[0][:40])
        counts[key] = counts.get(key, 0) + 1
        samples.setdefault(key, text[:150])

    analyzer = type("_Counts", (), {"counts": counts, "samples": samples})()

    # À nombre égal, l'élément dont l'extrait est le plus long est la vignette
    # produit — le prix seul vit dans un <span> minuscule.
    ranked = sorted(
        analyzer.counts.items(),
        key=lambda kv: (kv[1], len(analyzer.samples.get(kv[0], ""))),
        reverse=True,
    )
    candidates = [
        {
            "tag": tag,
            "class": css_class,
            "count": count,
            "sample": _redact_sample(analyzer.samples.get((tag, css_class), "")),
        }
        for (tag, css_class), count in ranked[:top]
    ]

    prices = re.findall(r"\d{1,3}[.,]\d{2}\s*€", html)
    embedded = re.findall(
        r"(window\.__[A-Z_]+__|__NEXT_DATA__|__NUXT__|application/json)", html
    )
    return {
        "page_size": len(html),
        "prices_in_page": len(prices),
        "embedded_json_markers": sorted(set(embedded)),
        "candidates": candidates,
    }


def _pack_from_unit_price(product: DriveProduct):
    """Retrouve le conditionnement à partir du prix unitaire affiché."""
    from ..units import Pack, UnknownUnit, canonical_unit

    if not product.unit_price_hint or not product.unit_hint_unit or not product.price_eur:
        return None
    if product.unit_price_hint <= 0:
        return None
    quantity = product.price_eur / product.unit_price_hint
    if not (0.01 <= quantity <= 200):        # au-delà, c'est une lecture ratée
        return None
    try:
        unit = canonical_unit(product.unit_hint_unit)
    except UnknownUnit:
        return None
    # Les formats réels sont ronds : 6 L, 0,5 kg, 24 rouleaux.
    rounded = round(quantity, 2)
    return Pack(rounded, unit)


def _cross_check(product: DriveProduct, pack) -> tuple[str | None, str | None]:
    """Compare notre calcul au prix unitaire affiché par l'enseigne.

    Renvoie ``(note, motif_suspect)``. Un petit écart se note ; un gros écart
    disqualifie l'offre — vécu : une litière 15 L « à 1,32 € » dont 1,32 €
    était en fait le prix AU LITRE, devenue record à 0,088 €/L dans le rapport.
    """
    from ..units import UnknownUnit, comparable_units, to_base

    try:
        if not comparable_units(product.unit_hint_unit, pack.unit):
            return None, None
        computed = product.price_eur / pack.total_base
        displayed = product.unit_price_hint / to_base(1, product.unit_hint_unit)
    except (UnknownUnit, ZeroDivisionError):
        return None, None
    if displayed <= 0:
        return None, None

    ecart = abs(computed - displayed) / displayed
    if ecart < 0.05:
        return None, None
    if ecart <= 0.25:
        return (
            f"⚠ écart de {ecart:.0%} entre notre calcul ({computed:.3f}) et le "
            f"prix unitaire affiché ({displayed:.3f}) — format à vérifier",
            None,
        )

    reason = (
        f"prix incohérent : {product.price_eur:.2f} € pour {pack.describe()} "
        f"donnerait {computed:.3f} €/{product.unit_hint_unit}, or l'enseigne "
        f"affiche {product.unit_price_hint:g} €/{product.unit_hint_unit}"
    )
    if abs(product.price_eur - product.unit_price_hint) < 0.01:
        probable = displayed * pack.total_base
        reason += (
            f" — le prix lu est probablement le prix unitaire ; "
            f"prix pack vraisemblable : {probable:.2f} €"
        )
    return None, reason


def _redact_sample(text: str) -> str:
    """Un extrait peut contenir un nom ou une adresse de retrait."""
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)
    return re.sub(r"\b\d{9,}\b", "[numéro]", text)

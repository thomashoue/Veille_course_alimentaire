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
from ..ingest.html_extract import extract_jsonld, find_blocks, parse_price, to_text
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
_BLOCK_PATTERNS = [
    ("li", "produit"),
    ("li", "product"),
    ("div", "produit"),
    ("div", "product"),
    ("article", "product"),
    ("article", None),
    ("li", None),
]


def _clean_label(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    # Le prix fait partie du texte du bloc : on le retire du libellé.
    text = re.sub(r"\d+[.,]\d{2}\s*€|\d+\s*€\s*\d{2}|€\s*\d+[.,]\d{2}", " ", text)
    text = re.sub(r"(?i)\b(ajouter|au panier|le kg|le l|prix au kilo|voir le produit)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -·|")


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
    """Dernier recours : repérer les blocs qui contiennent un prix ET un libellé."""
    for tag, needle in _BLOCK_PATTERNS:
        found: list[DriveProduct] = []
        for block in find_blocks(html, tag, needle):
            text = block.text
            if not (MIN_BLOCK_TEXT <= len(text) <= MAX_BLOCK_TEXT):
                continue
            price = parse_price(text)
            label = _clean_label(text)
            if price is None or len(label) < 5:
                continue
            found.append(
                DriveProduct(
                    ref=(block.attrs.get("data-ref") or block.attrs.get("id") or label)[:80],
                    label=label[:140],
                    price_eur=price,
                    available="indisponible" not in strip_accents(text).lower(),
                    url=block.links[0] if block.links else None,
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

    for product in products:
        item = config.match_item(product.label)
        if item is None:
            unmatched += 1
            continue
        if product.price_eur is None:
            continue
        pack = product.pack or parse_pack(product.label)
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
                source=Source.DRIVE.value,
                # Mérité : cette page vient bien du drive, chargée par l'humain.
                verified_in_drive=True,
                available=product.available,
                source_url=source_url,
                banner=store.banner,
                drive_ref=product.ref,
                notes=[f"lu dans une page enregistrée ({method})"],
            )
        )

    report = {
        "method": method,
        "products_found": len(products),
        "matched_to_basket": len(observations),
        "ignored_not_in_basket": unmatched,
        "page_size": len(html),
        "page_text_size": len(to_text(html)),
    }
    return observations, report


# --------------------------------------------------------------------------- #
# Diagnostic de gabarit
# --------------------------------------------------------------------------- #
class _StructureAnalyzer(HTMLParser):
    """Recense les éléments qui portent un prix, par (balise, classe).

    Sert à découvrir le gabarit d'un drive qu'on n'a jamais vu : la paire la
    plus fréquente qui contient un prix ET un libellé, c'est la vignette
    produit. On lit la structure, pas le contenu.
    """

    SKIP = {"script", "style", "noscript", "head"}
    MAX_TEXT = 400

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.counts: dict[tuple[str, str], int] = {}
        self.samples: dict[tuple[str, str], str] = {}
        self._stack: list[dict] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in self.SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        attributes = {k: (v or "") for k, v in attrs}
        classes = attributes.get("class", "") or attributes.get("id", "")
        self._stack.append({"tag": tag, "class": classes, "text": []})

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip or not self._stack:
            return
        # Retrouve l'ouverture correspondante (les pages réelles sont mal formées).
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index]["tag"] == tag:
                node = self._stack.pop(index)
                del self._stack[index:]
                break
        else:
            return

        text = re.sub(r"\s+", " ", " ".join(node["text"])).strip()
        if self._stack:
            self._stack[-1]["text"].append(text)
        if not node["class"] or not (MIN_BLOCK_TEXT <= len(text) <= self.MAX_TEXT):
            return
        if parse_price(text) is None:
            return

        # Une classe utilisable : on garde la première, la plus stable.
        key = (node["tag"], node["class"].split()[0][:40])
        self.counts[key] = self.counts.get(key, 0) + 1
        self.samples.setdefault(key, text[:150])

    def handle_data(self, data):  # noqa: ANN001
        if not self._skip and self._stack:
            self._stack[-1]["text"].append(data)


def analyze_page(html: str, top: int = 12) -> dict:
    """Découvre le gabarit d'une page inconnue.

    Ne renvoie que de la structure et des extraits courts, pour pouvoir être
    recopié dans une conversation sans exposer un compte.
    """
    analyzer = _StructureAnalyzer()
    try:
        analyzer.feed(html)
    except Exception as exc:
        log.warning("analyse interrompue : %s", exc)

    ranked = sorted(analyzer.counts.items(), key=lambda kv: kv[1], reverse=True)
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


def _redact_sample(text: str) -> str:
    """Un extrait peut contenir un nom ou une adresse de retrait."""
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)
    return re.sub(r"\b\d{9,}\b", "[numéro]", text)

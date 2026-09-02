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
    detect_mechanic,
    detect_weight_basis,
    extract_jsonld,
    parse_pack_price,
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

# Bannières marketing collées au libellé (Courses U : « ICI PRIX MINI »,
# « PRIX BAS »…). Retirées avant le repli du libellé dupliqué.
_PROMO_BANNERS = re.compile(
    r"(?i)\b(ici prix mini|prix bas|prix mini|prix choc|bon plan|prix ronds?"
    r"|nouveaut[eé]s?|nouveau|la promo|promo|offre|top affaire)\b"
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
    # Sous-titre de conditionnement Intermarché (« … la boîte de 90g net
    # égoutté • », « le sachet de 1kg • ») : coupe au descripteur.
    desc = re.search(
        r"(?i)\b(la bo[iî]te|le sachet|le pack|le lot|la barquette|le paquet|les? \d)\b",
        text,
    )
    if desc and desc.start() >= 5:
        text = text[: desc.start()]
    # D'abord les prix unitaires (« 0,95 €/l », « 3,07 € le kg ») : les retirer
    # en entier évite de laisser traîner un « /l » orphelin dans le libellé.
    text = re.sub(r"\d+[.,]\d+\s*€\s*(?:/|le|par|au)\s*[a-zéè]{1,10}\b", " ", text, flags=re.I)
    # Reste des prix et du sélecteur de quantité pour les gabarits sans bouton.
    text = re.sub(r"\d+[.,]\d{2}\s*€|\d+\s*€\s*,?\s*\d{2}|€\s*\d+[.,]\d{2}|\d+\s*€", " ", text)
    text = re.sub(r"(?i)\b(le kg|le l|par kg|prix au kilo|sponsoris\w*)\b", " ", text)
    text = re.sub(r"(?<= )[-+](?= )|\s0\s\+", " ", text)
    # On ne rogne pas le « + » collé : « Eco+ » est une marque, pas un séparateur.
    # Bannières marketing (ICI PRIX MINI, PRIX BAS…) : elles décalaient le repli
    # du doublon et polluaient le libellé.
    text = _PROMO_BANNERS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" -·|")
    # Chez U, l'image et le lien portent le même texte : « Nom sac 5L Nom sac
    # 5L ». On replie le doublon, même s'il reste un mot parasite au milieu.
    words = text.split()
    n = len(words)
    for k in range(n // 2, 1, -1):
        if words[:k] == words[k : 2 * k]:
            text = " ".join(words[:k] + words[2 * k :])
            break
    return text.strip(" -·|")


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


_NAME_KEYS = ("libelle", "label", "name", "title", "productname", "nom", "denomination")
_PRICE_KEYS = ("prix", "price", "prixunitaire", "grossamount", "sellingprice",
               "currentprice", "pricevalue", "amount")


def _coerce_price(value) -> float | None:
    """Lit un prix quel que soit son emballage : nombre, texte, ou objet
    ({value|amount|formattedValue}, ou centAmount entier en centimes)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        import re as _re
        m = _re.search(r"\d+[.,]\d{2}", value)
        return float(m.group().replace(",", ".")) if m else None
    if isinstance(value, dict):
        low = {k.lower(): v for k, v in value.items()}
        if "centamount" in low:
            try:
                return int(low["centamount"]) / 100.0
            except (ValueError, TypeError):
                pass
        for key in ("formattedvalue", "value", "amount", "grossamount"):
            if key in low:
                p = _coerce_price(low[key])
                if p is not None:
                    return p
    return None


def _json_blobs(html: str) -> list:
    """JSON embarqués : <script type=application/json> (Next.js __NEXT_DATA__,
    Nuxt…) ET assignations window.__X__ = {…}."""
    blobs: list = []
    for m in re.finditer(
        r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I,
    ):
        try:
            blobs.append(json.loads(m.group(1).strip()))
        except (json.JSONDecodeError, ValueError):
            pass
    for m in re.finditer(
        r"(?:window\.__[A-Z_]+__|__NUXT__|dataLayer)\s*=\s*(\{.*?\})\s*[;<]",
        html, re.S,
    ):
        try:
            blobs.append(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            pass
    return blobs


def products_from_embedded_json(html: str) -> list[DriveProduct]:
    """Beaucoup de drives déposent leur catalogue dans un JSON inline.

    C'est la même donnée que celle du XHR, déjà présente dans la page : la voie
    la plus fiable après le JSON-LD.
    """
    products: list[DriveProduct] = []
    for data in _json_blobs(html):
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            low = {k.lower(): v for k, v in node.items()}
            label = next(
                (low[k] for k in _NAME_KEYS if isinstance(low.get(k), str) and low[k].strip()),
                None,
            )
            price = None
            for k in _PRICE_KEYS:
                if k in low:
                    price = _coerce_price(low[k])
                    if price is not None:
                        break
            if not label or price is None or price <= 0 or price > 500:
                continue
            products.append(
                DriveProduct(
                    ref=str(node.get("id") or node.get("sku") or node.get("ref") or label)[:80],
                    label=label.strip(),
                    price_eur=price,
                    available=bool(low.get("disponible", low.get("available", True))),
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
            price = parse_price(price_text) if price_text else parse_pack_price(text)
            regular_text = _child_text(tile, _REGULAR_CHILD)
            regular = parse_price(regular_text) if regular_text else None
            label = _clean_label(text)
            if len(label) < 5:
                continue
            unit_hint = parse_unit_price(_child_text(tile, _UNIT_CHILD) or text)
            # Aucun prix de pack, mais un €/kg et un grammage (cas Intermarché) :
            # on reconstruit le prix de la boîte, que le site donne implicitement.
            if price is None and unit_hint is not None:
                pack = parse_pack(label)
                if pack is not None:
                    from ..units import to_base
                    try:
                        base_per_unit = to_base(1, unit_hint[1])
                        price = round(unit_hint[0] / base_per_unit * pack.total_base, 2)
                    except Exception:
                        price = None
            if price is None:
                continue
            found.append(
                DriveProduct(
                    ref=(tile["attrs"].get("data-ref")
                         or tile["attrs"].get("data-itemid")
                         or tile["attrs"].get("id") or label)[:80],
                    label=label[:140],
                    # Conditionnement lu sur la vignette ENTIÈRE : le grammage
                    # vit dans le sous-titre qu'on retire du libellé.
                    pack=parse_pack(text),
                    price_eur=price,
                    regular_price=regular,
                    available="indisponible" not in strip_accents(text).lower(),
                    unit_price_hint=unit_hint[0] if unit_hint else None,
                    unit_hint_unit=unit_hint[1] if unit_hint else None,
                    # Base de poids et mécanique lues sur le texte ENTIER de la
                    # vignette : le nettoyage du libellé les efface.
                    weight_basis=detect_weight_basis(text),
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
            key = (strip_accents(product.label).lower().split(), product.price_eur)
            unique.setdefault(str(key), product)
        if unique:
            return list(unique.values()), method
    return [], "aucune"


# Domaine de drive → enseigne. Une page SingleFile embarque l'URL source, donc
# le domaine est présent et fiable pour reconnaître l'enseigne.
_DRIVE_DOMAINS = {
    "leclercdrive.fr": "leclerc",
    "coursesu.com": "u",
    "intermarche.com": "intermarche",
    "lidl.fr": "lidl",
    "aldi-marche.fr": "aldi",
    "aldi.fr": "aldi",
}
# Repli sur des marqueurs de gabarit si le domaine manque.
_GABARIT_MARKERS = {
    "leclerc": ("wcrs310", "leclercdrive"),
    "intermarche": ("stime-product", "intermarche.com"),
    "u": ("coursesu", "price-sales", "product-tile__name"),
}


def detect_store(html: str, config: Config) -> str | None:
    """Reconnaît le magasin d'une page enregistrée, pour trier un dossier mêlé.

    Ordre : URL de drive exacte du référentiel, puis enseigne par domaine ou
    marqueur de gabarit, puis désambiguïsation par la ville quand plusieurs
    magasins partagent l'enseigne.
    """
    low = (html or "").lower()

    # URL de drive du référentiel, la PLUS SPÉCIFIQUE d'abord : l'URL complète
    # d'Yffiniac (/drive-hyperu-yffiniac) doit l'emporter sur le coursesu.com
    # générique d'un autre magasin U.
    # Tous les magasins autorisés (drive ET liste papier comme Lidl/Aldi), URL
    # la plus spécifique d'abord.
    stores = config.allowed_stores()
    for store in sorted(stores, key=lambda s: len(s.drive_base_url or ""), reverse=True):
        base = (store.drive_base_url or "").lower()
        offers = (store.offers_url or "").lower()
        if base and base in low:
            return store.id
        if offers and offers.split("?")[0] in low:
            return store.id

    banner = None
    for domain, b in _DRIVE_DOMAINS.items():
        if domain in low:
            banner = b
            break
    if banner is None:
        for b, markers in _GABARIT_MARKERS.items():
            if any(m in low for m in markers):
                banner = b
                break
    if banner is None:
        return None

    candidates = [s for s in config.allowed_stores() if s.banner == banner]
    if len(candidates) == 1:
        return candidates[0].id
    if candidates:
        alnum = re.sub(r"[^a-z0-9]+", "", strip_accents(to_text(html)).lower())
        for store in candidates:
            city = re.sub(r"[^a-z0-9]+", "", strip_accents(store.city).lower())
            if city and city in alnum:
                return store.id
        return candidates[0].id
    return None


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
                # Intermarché affiche en net égoutté, Leclerc en brut (P5) :
                # lu sur la vignette complète (le nettoyage du libellé l'efface).
                weight_basis=product.weight_basis or detect_weight_basis(product.label),
                mechanic=detect_mechanic(product.label),
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

    import re as _re
    def _alnum(t): return _re.sub(r"[^a-z0-9]+", "", strip_accents(t).lower())
    page_alnum = _alnum(to_text(html))
    report = {
        "method": method,
        # Piège Intermarché vécu : se connecter à un autre magasin bascule le
        # magasin actif de TOUT le compte. Si la ville attendue n'apparaît
        # nulle part dans la page, les prix sont peut-être ceux d'un autre
        # magasin — à vérifier avant de s'en servir.
        "store_city_seen": True if not store.drive_base_url else ((_alnum(store.city) in page_alnum) if store.city else True),
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
def json_shape_report(html: str, top: int = 8) -> list[dict]:
    """Révèle la FORME des objets d'un JSON embarqué (Next.js/Nuxt).

    Recense les dicts qui ressemblent à des produits (une chaîne + un nombre ou
    un prix), groupés par jeu de clés, avec un échantillon. Sert à découvrir les
    noms de champs réels d'un site comme Aldi sans coller tout le __NEXT_DATA__.
    """
    from collections import Counter

    shapes: Counter = Counter()
    samples: dict = {}
    for data in _json_blobs(html):
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            has_str = any(isinstance(v, str) and v.strip() for v in node.values())
            has_num = any(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in node.values()
            ) or any(isinstance(v, dict) for v in node.values())
            if len(node) < 3 or not (has_str and has_num):
                continue
            key = tuple(sorted(node.keys()))[:20]
            shapes[key] += 1
            if key not in samples:
                samples[key] = {
                    k: (str(v)[:40] if not isinstance(v, (dict, list)) else type(v).__name__)
                    for k, v in list(node.items())[:12]
                }
    out = []
    for keys, count in shapes.most_common(top):
        out.append({"count": count, "keys": list(keys), "sample": samples.get(keys, {})})
    return out


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

    ratio = computed / displayed
    if 0.85 <= ratio <= 1.05:
        return None, None

    # Le seul cas qui DISQUALIFIE : notre calcul est très en dessous du prix
    # unitaire affiché (ratio << 1). Signature du bug litière : le prix lu était
    # en fait le prix AU LITRE, divisé par un gros grammage → €/L minuscule.
    if ratio < 0.5:
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

    # Notre calcul est AU-DESSUS du €/kg affiché (ratio > 1) : cas classique du
    # net égoutté (l'enseigne affiche au poids brut, nous au net, plus petit et
    # plus cher au kilo). Légitime — on note, on ne disqualifie pas.
    if computed > displayed:
        return (
            f"€/kg calculé sur le net ({computed:.2f}), l'enseigne affiche "
            f"{displayed:.2f} au brut",
            None,
        )
    return (
        f"⚠ écart avec le prix unitaire affiché ({computed:.3f} vs "
        f"{displayed:.3f}) — format à vérifier",
        None,
    )


def _redact_sample(text: str) -> str:
    """Un extrait peut contenir un nom ou une adresse de retrait."""
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)
    return re.sub(r"\b\d{9,}\b", "[numéro]", text)

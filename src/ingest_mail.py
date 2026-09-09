"""Veille des e-mails de commande : capter ce qu'on a VRAIMENT acheté.

Un e-mail de confirmation de commande drive (« Votre commande est validée ! »)
contient la liste exacte des produits, quantités et prix payés. On le lit pour
alimenter l'historique (le ledger) sans re-saisir quoi que ce soit : c'est là
que l'outil apprend vos habitudes, semaine après semaine.

Aujourd'hui le format Intermarché drive est géré. Chaque bloc produit y suit le
même motif : lignes de nom, ligne de format (« le sachet de 900g »), ligne de
quantité (« x1 » ou « x0.3 kg » en vrac), prix unitaire, prix total.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .config import Config, _contains_word, _normalize
from .models import PriceObservation

# Une ligne de prix seule : « 3,71 € ». Sert d'ancre et de séparateur.
_PRICE = re.compile(r"^\s*(\d+(?:[.,]\d{1,2})?)\s*€\s*$")
# Une ligne de quantité : « x1 », « x2 », ou vrac « x0.3 kg ».
_QTY = re.compile(r"^\s*x\s*(\d+(?:[.,]\d+)?)\s*(kg|g|l|ml|cl)?\s*$", re.IGNORECASE)
# Bruit d'en-tête / pied de page à ne jamais prendre pour un nom de produit.
_HEADER_NOISE = re.compile(
    r"€|disponible|cagnotte|vous avez|total|n°\s*de\s*commande|tél|produits?\s|"
    r"message transféré|^de\s*:|^à\s*:|^cc\s*:|gmail|http|@",
    re.IGNORECASE,
)
_UNIT_TO_BASE = {"kg": ("kg", 1.0), "g": ("kg", 0.001),
                 "l": ("L", 1.0), "ml": ("L", 0.001), "cl": ("L", 0.01)}


@dataclass
class OrderLine:
    name: str
    fmt: str
    qty_label: str
    unit_price: float
    total_price: float
    basket_item_id: str | None = None
    pack_size: float | None = None
    pack_unit: str | None = None
    pack_count: int = 1
    weight_basis: str | None = None


@dataclass
class Order:
    store_id: str | None
    order_date: date
    reference: str | None
    total_eur: float | None
    lines: list[OrderLine] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
def _num(text: str) -> float:
    return float(text.replace(",", ".").replace(" ", ""))


def _strip_html(raw: str) -> str:
    if "<" not in raw or ">" not in raw:
        return raw
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|td|li|h\d)>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&eacute;", "é"),
                 ("&egrave;", "è"), ("&agrave;", "à"), ("&#39;", "'"), ("&quot;", '"')):
        text = text.replace(a, b)
    return text


def read_email(path: Path | str) -> str:
    """Lit un e-mail (.eml, .html, .txt) et rend son texte."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    if str(path).lower().endswith(".eml") or raw[:200].lower().lstrip().startswith(("from:", "return-path:", "received:")):
        import email
        from email import policy
        msg = email.message_from_string(raw, policy=policy.default)
        body = msg.get_body(preferencelist=("plain", "html"))
        if body is not None:
            content = body.get_content()
            return _strip_html(content) if body.get_content_type() == "text/html" else content
    return _strip_html(raw)


# --------------------------------------------------------------------------- #
def detect_store(text: str, config: Config) -> str | None:
    """Reconnaît le magasin : enseigne (expéditeur) + ville, via la config.

    Recherche insensible aux accents (« Intermarché » = « intermarche ») et par
    mot entier (la bannière « u » ne matche pas le moindre « u » du texte).
    """
    norm = _normalize(text)
    seen = {s.banner.lower() for s in config.stores.values()
            if s.banner and _contains_word(norm, s.banner)}
    for store in config.stores.values():           # enseigne + ville : le mieux
        if store.banner.lower() in seen and store.city and _normalize(store.city) in norm:
            return store.id
    for store in config.stores.values():            # enseigne seule, ville incertaine
        if store.banner.lower() in seen:
            return store.id
    return None


def _order_date(text: str) -> date:
    m = re.search(r"Disponible le\s*(\d{2})/(\d{2})/(\d{4})", text)
    if m:
        d, mo, y = (int(g) for g in m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            pass
    return date.today()


def _extract_pack(line: OrderLine, item_unit: str) -> None:
    """Déduit (pack_size, pack_unit, pack_count) du format, selon l'unité voulue.

    L'unité de l'article décide de ce qu'on lit : un poids (kg), un volume (L)
    ou un nombre (unité). En vrac (« x0.3 kg »), le prix est déjà au kg.
    """
    qty = _QTY.match(line.qty_label)
    if qty and qty.group(2):                       # vrac : prix à l'unité de mesure
        base, factor = _UNIT_TO_BASE[qty.group(2).lower()]
        line.pack_size, line.pack_unit, line.pack_count = 1.0, base, 1
        return
    count = int(float(qty.group(1))) if qty else 1  # nb de packs achetés (info)

    fmt = line.fmt.lower()
    if "net égoutté" in fmt or "net egoutte" in fmt:
        line.weight_basis = "net_egoutte"

    if item_unit in ("kg", "L"):
        wanted = {"kg": ("kg", "g"), "L": ("l", "ml", "cl")}[item_unit]
        # « N pots/pièces … de M g » → total = N × M
        mult = re.search(r"(\d+)\s*(?:pots?|pièces?|sachets?|tranches?|x)\D{0,8}de\s*(\d+(?:[.,]\d+)?)\s*(kg|g|l|ml|cl)", fmt)
        if mult and mult.group(3).lower() in _UNIT_TO_BASE:
            base, factor = _UNIT_TO_BASE[mult.group(3).lower()]
            if base == item_unit:
                line.pack_size, line.pack_unit, line.pack_count = _num(mult.group(2)) * factor * int(mult.group(1)), base, 1
                return
        # sinon : tous les poids/volumes du format, on prend le plus grand (total)
        vals = []
        for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(kg|g|l|ml|cl)", fmt):
            u = m.group(2).lower()
            if u in _UNIT_TO_BASE:
                base, factor = _UNIT_TO_BASE[u]
                if base == item_unit:
                    vals.append(_num(m.group(1)) * factor)
        if vals:
            line.pack_size, line.pack_unit, line.pack_count = max(vals), item_unit, 1
        return

    # item_unit == "unite" : chercher un nombre de pièces (« boite de 20 »,
    # « paquet de 4 »), sinon un pack.
    m = re.search(r"\bde\s*(\d+)\b(?!\s*(?:g|kg|ml|cl|l)\b)", fmt)
    if m:
        line.pack_size, line.pack_unit, line.pack_count = 1.0, "unite", int(m.group(1))
    else:
        line.pack_size, line.pack_unit, line.pack_count = 1.0, "unite", 1


def parse_order(text: str, config: Config) -> Order:
    """Extrait la commande d'un e-mail de confirmation drive."""
    store_id = detect_store(text, config)
    ref_m = re.search(r"N°\s*de\s*commande\s*(\d+)", text)
    total_m = re.search(r"total[^\n:]*:?\s*(\d+(?:[.,]\d{2}))\s*€", text, re.IGNORECASE)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    order = Order(
        store_id=store_id,
        order_date=_order_date(text),
        reference=ref_m.group(1) if ref_m else None,
        total_eur=_num(total_m.group(1)) if total_m else None,
    )

    pending: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if (_QTY.match(line) and i + 2 < len(lines)
                and _PRICE.match(lines[i + 1]) and _PRICE.match(lines[i + 2])):
            fmt = pending[-1] if pending else ""
            name = " ".join(pending[:-1]) if len(pending) > 1 else (pending[0] if pending else "")
            ol = OrderLine(
                name=name.strip(), fmt=fmt.strip(), qty_label=line,
                unit_price=_num(_PRICE.match(lines[i + 1]).group(1)),
                total_price=_num(_PRICE.match(lines[i + 2]).group(1)),
            )
            item = config.match_item(f"{name} {fmt}", include_out_of_scope=True)
            if item is None:
                order.unmatched.append(f"{name} — {fmt}".strip(" —"))
            else:
                ol.basket_item_id = item.id
                _extract_pack(ol, item.base_unit if hasattr(item, "base_unit") else item.unit)
                order.lines.append(ol)
            pending = []
            i += 3
            continue
        # Prix seul ou bruit d'en-tête : sépare les blocs (protège le 1er nom).
        if _PRICE.match(line) or _HEADER_NOISE.search(line):
            pending = []
        else:
            pending.append(line)
        i += 1
    return order


# --------------------------------------------------------------------------- #
def to_observations(order: Order, config: Config) -> list[PriceObservation]:
    """Transforme les lignes d'une commande en observations pour le ledger."""
    if not order.store_id:
        return []
    observed = datetime.combine(order.order_date, datetime.min.time())
    out: list[PriceObservation] = []
    for ol in order.lines:
        obs = PriceObservation(
            store_id=order.store_id,
            basket_item_id=ol.basket_item_id,
            product_label=f"{ol.name} — {ol.fmt}".strip(" —"),
            price_eur=ol.unit_price,
            observed_at=observed,
            category=config.items[ol.basket_item_id].category,
            pack_size=ol.pack_size,
            pack_unit=ol.pack_unit,
            pack_count=ol.pack_count,
            weight_basis=ol.weight_basis,
            source="drive",
            verified_in_drive=True,
            requires_drive_verification=True,
            banner=config.store(order.store_id).banner,
            available=True,
            notes=[f"e-mail de commande {order.reference or ''}".strip()],
        )
        out.append(obs)
    return out

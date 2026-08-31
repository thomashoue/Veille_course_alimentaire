"""Courses U — client drive (backlog P3).

Rappel du §6 : sans magasin sélectionné, coursesu.com masque les prix. Le
client vérifie donc que la page affiche bien des prix avant de conclure quoi
que ce soit ; une page sans prix n'est pas « produit absent », c'est une
session incomplète.

Les offres U ne sont accessibles autrement : c'est le drive le plus utile à
connecter en priorité (§10).
"""

from __future__ import annotations

import logging
import re

from .base import CartLine, DriveClient, DriveError, DriveProduct
from .session import BrowserSession

log = logging.getLogger(__name__)


class CoursesUDrive(DriveClient):
    banner = "u"

    def __init__(self, store, session: BrowserSession | None = None, headless: bool = True, **kwargs):
        super().__init__(store, **kwargs)
        self.session = session or BrowserSession("u", headless=headless)
        self._page = None

    def page(self):
        if self._page is None:
            self._page = self.session.page()
        return self._page

    def close(self) -> None:
        self._page = None
        self.session.close()

    # ------------------------------------------------------------------ #
    def search(self, query: str, limit: int = 12) -> list[DriveProduct]:
        page = self.page()
        page.goto(self.store.search_url(query), wait_until="domcontentloaded")
        script = """
        () => Array.from(document.querySelectorAll(
            'article[class*=product], li[class*=product], [data-testid*=product]'
        )).map(n => {
            const t = (s) => { const e = n.querySelector(s); return e ? e.textContent.trim() : null; };
            return {
                ref: n.getAttribute('data-id') || n.id || '',
                label: t('[class*=title], [class*=label], h3') || n.textContent.trim().slice(0, 140),
                price: t('[class*=price]') || '',
                available: !n.textContent.toLowerCase().includes('indisponible')
            };
        })
        """
        try:
            raw = page.evaluate(script) or []
        except Exception as exc:
            log.warning("coursesu : lecture du DOM impossible (%s)", exc)
            return []

        products = []
        for entry in raw:
            label = (entry.get("label") or "").strip()
            if not label:
                continue
            products.append(
                DriveProduct(
                    ref=str(entry.get("ref") or label)[:80],
                    label=label,
                    price_eur=_price(entry.get("price")),
                    available=bool(entry.get("available", True)),
                    url=page.url,
                    raw=entry,
                )
            )
        if products and not any(p.price_eur for p in products):
            raise DriveError(
                "coursesu.com renvoie des produits sans prix : aucun magasin "
                "sélectionné dans la session. Lancez "
                "`python -m src.cli login --banner u` et choisissez "
                f"{self.store.name}."
            )
        return products[:limit]

    def cart_state(self) -> list[CartLine]:
        page = self.page()
        page.goto(self.store.cart_url() or "https://www.coursesu.com/panier", wait_until="domcontentloaded")
        script = """
        () => Array.from(document.querySelectorAll('[class*=cart-item], li[class*=basket]')).map(n => {
            const t = (s) => { const e = n.querySelector(s); return e ? e.textContent.trim() : null; };
            const q = n.querySelector('input[type=number]');
            return {
                ref: n.getAttribute('data-id') || n.id || '',
                label: t('[class*=title], [class*=label]') || n.textContent.trim().slice(0, 140),
                qty: q ? q.value : '1',
                price: t('[class*=price]') || ''
            };
        })
        """
        try:
            raw = page.evaluate(script) or []
        except Exception as exc:
            raise DriveError(f"lecture du panier impossible : {exc}") from exc
        return [
            CartLine(
                ref=str(e.get("ref") or e.get("label"))[:80],
                label=(e.get("label") or "").strip(),
                quantity=_int(e.get("qty")),
                price_eur=_price(e.get("price")),
            )
            for e in raw
            if (e.get("label") or "").strip()
        ]

    def _add(self, product: DriveProduct, quantity: int) -> None:
        page = self.page()
        selector = f'article:has-text("{product.label[:40]}") button:has-text("Ajouter")'
        for _ in range(max(1, quantity)):
            try:
                page.click(selector, timeout=15_000)
                page.wait_for_timeout(600)
            except Exception as exc:
                raise DriveError(f"clic d'ajout impossible sur {product.label!r} : {exc}") from exc


def _price(raw: str | None) -> float | None:
    if not raw:
        return None
    match = re.search(r"(\d{1,4})[.,](\d{2})", raw)
    return float(f"{match.group(1)}.{match.group(2)}") if match else None


def _int(raw: str | None) -> int:
    match = re.search(r"\d+", str(raw or "1"))
    return int(match.group()) if match else 1

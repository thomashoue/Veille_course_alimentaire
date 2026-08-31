"""Intermarché — client drive (backlog P3).

Deux pièges connus (§6, §7) :
  * ``/drive/<code>`` renvoie 404 : la seule URL qui marche est
    ``/recherche/{q}`` ;
  * se connecter à un autre magasin BASCULE le magasin actif de tout le
    compte. On vérifie donc le magasin actif avant toute lecture, et on refuse
    de travailler si ce n'est pas celui attendu, plutôt que de remplir le
    panier du mauvais magasin.
"""

from __future__ import annotations

import logging
import re

from ..units import strip_accents
from .base import CartLine, DriveClient, DriveError, DriveProduct
from .session import BrowserSession

log = logging.getLogger(__name__)


class IntermarcheDrive(DriveClient):
    banner = "intermarche"

    def __init__(self, store, session: BrowserSession | None = None, headless: bool = True, **kwargs):
        super().__init__(store, **kwargs)
        self.session = session or BrowserSession("intermarche", headless=headless)
        self._page = None

    def page(self):
        if self._page is None:
            self._page = self.session.page()
        return self._page

    def close(self) -> None:
        self._page = None
        self.session.close()

    # ------------------------------------------------------------------ #
    def active_store_label(self) -> str | None:
        page = self.page()
        try:
            return page.evaluate(
                "() => { const e = document.querySelector('[class*=store-name], [data-testid*=store]');"
                " return e ? e.textContent.trim() : null; }"
            )
        except Exception:
            return None

    def check_active_store(self) -> None:
        """Refuse de travailler si le magasin actif n'est pas celui attendu."""
        label = self.active_store_label()
        if not label:
            return
        expected = strip_accents(self.store.city).lower()
        if expected and expected not in strip_accents(label).lower():
            raise DriveError(
                f"magasin actif « {label} » ≠ {self.store.name} — "
                "se connecter à un autre magasin bascule tout le compte. "
                "Rebasculez à la main avant de relancer."
            )

    # ------------------------------------------------------------------ #
    def search(self, query: str, limit: int = 12) -> list[DriveProduct]:
        url = self.store.search_url(query)
        page = self.page()
        page.goto(url, wait_until="domcontentloaded")
        self.check_active_store()
        script = """
        () => Array.from(document.querySelectorAll(
            '[data-testid*=product-item], article[class*=product], li[class*=product]'
        )).map(n => {
            const t = (s) => { const e = n.querySelector(s); return e ? e.textContent.trim() : null; };
            return {
                ref: n.getAttribute('data-product-id') || n.id || '',
                label: t('[class*=title], [class*=name], h3') || n.textContent.trim().slice(0, 140),
                price: t('[class*=price]') || '',
                available: !n.textContent.toLowerCase().includes('indisponible')
            };
        })
        """
        try:
            raw = page.evaluate(script)
        except Exception as exc:
            log.warning("intermarche : lecture du DOM impossible (%s)", exc)
            return []
        products = []
        for entry in raw or []:
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
        return products[:limit]

    def cart_state(self) -> list[CartLine]:
        page = self.page()
        page.goto("https://www.intermarche.com/panier", wait_until="domcontentloaded")
        self.check_active_store()
        script = """
        () => Array.from(document.querySelectorAll('[data-testid*=cart-item], li[class*=cart]')).map(n => {
            const t = (s) => { const e = n.querySelector(s); return e ? e.textContent.trim() : null; };
            const q = n.querySelector('input[type=number]');
            return {
                ref: n.getAttribute('data-product-id') || n.id || '',
                label: t('[class*=title], [class*=name]') || n.textContent.trim().slice(0, 140),
                qty: q ? q.value : '1',
                price: t('[class*=price]') || ''
            };
        })
        """
        try:
            raw = page.evaluate(script)
        except Exception as exc:
            raise DriveError(f"lecture du panier impossible : {exc}") from exc
        return [
            CartLine(
                ref=str(e.get("ref") or e.get("label"))[:80],
                label=(e.get("label") or "").strip(),
                quantity=_int(e.get("qty")),
                price_eur=_price(e.get("price")),
            )
            for e in (raw or [])
            if (e.get("label") or "").strip()
        ]

    def _add(self, product: DriveProduct, quantity: int) -> None:
        page = self.page()
        selector = f'[data-testid*=product-item]:has-text("{product.label[:40]}") button:has-text("Ajouter")'
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

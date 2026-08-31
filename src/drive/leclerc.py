"""E.Leclerc Drive — client principal (backlog P1).

Pièges traités, tous constatés pendant l'expérimentation (§7) :
  * la session ne se propage vers ``fd7-courses`` qu'après « Commencer mes
    courses » : on le détecte et on le dit, au lieu de rendre un panier vide ;
  * les pages « Promotions » sont des IMAGES sans texte : on ne les lit pas,
    on passe par des recherches produit par produit ;
  * les clics par référence échouent silencieusement : chaque mutation du
    panier est re-vérifiée par relecture (logique du socle) ;
  * le site est lent : timeouts généreux.

On privilégie l'appel XHR de recherche quand il répond ; le DOM n'est que le
filet de sécurité.
"""

from __future__ import annotations

import json
import logging
import re

from .base import CartLine, DriveClient, DriveError, DriveProduct
from .session import BrowserSession

log = logging.getLogger(__name__)


class LeclercDrive(DriveClient):
    banner = "leclerc"

    def __init__(self, store, session: BrowserSession | None = None, headless: bool = True, **kwargs):
        super().__init__(store, **kwargs)
        self.session = session or BrowserSession("leclerc", headless=headless)
        self._page = None

    # ------------------------------------------------------------------ #
    def page(self):
        if self._page is None:
            self._page = self.session.page()
        return self._page

    def close(self) -> None:
        self._page = None
        self.session.close()

    def _ensure_courses_session(self) -> None:
        """Vérifie que la session est bien propagée vers le sous-domaine drive."""
        page = self.page()
        base = (self.store.drive_base_url or "").rstrip("/")
        if base and not page.url.startswith(base):
            page.goto(base, wait_until="domcontentloaded")
        content = page.content()
        if "Commencer mes courses" in content and "panier" not in page.url.lower():
            raise DriveError(
                "session non propagée vers fd7-courses : ouvrez "
                "`python -m src.cli login --banner leclerc` et cliquez "
                "« Commencer mes courses »."
            )

    # ------------------------------------------------------------------ #
    def search(self, query: str, limit: int = 12) -> list[DriveProduct]:
        url = self.store.search_url(query)
        if not url:
            raise DriveError(f"pas d'URL de recherche pour {self.store.id}")
        page = self.page()
        page.goto(url, wait_until="domcontentloaded")
        self._ensure_courses_session()

        products = self._products_from_dom(page)
        return products[:limit]

    def _products_from_dom(self, page) -> list[DriveProduct]:
        """Lit les vignettes produit. Le sélecteur est isolé ici, exprès :
        c'est la seule chose à corriger quand le gabarit change."""
        script = """
        () => Array.from(document.querySelectorAll(
            '[id^="ctl00_"] .liste-produits li, .liste-produits li, li.resultat-produit, article.produit'
        )).map(node => {
            const text = (sel) => {
                const el = node.querySelector(sel);
                return el ? el.textContent.trim() : null;
            };
            const button = node.querySelector('button, input[type=submit], a.ajouter');
            return {
                ref: node.getAttribute('data-ref') || node.id || (text('.libelle') || '').slice(0, 60),
                label: text('.libelle') || text('.nom-produit') || node.textContent.trim().slice(0, 140),
                price: text('.prix') || text('.prix-unitaire') || '',
                unit: text('.prix-par-unite') || '',
                available: !node.className.toLowerCase().includes('indisponible'),
                addSelector: button ? true : false
            };
        })
        """
        try:
            raw = page.evaluate(script)
        except Exception as exc:                     # injection en timeout : §7
            log.warning("leclerc : lecture du DOM impossible (%s)", exc)
            return []
        products: list[DriveProduct] = []
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
        return products

    # ------------------------------------------------------------------ #
    def cart_state(self) -> list[CartLine]:
        url = self.store.cart_url()
        if not url:
            raise DriveError(f"pas d'URL de panier pour {self.store.id}")
        page = self.page()
        page.goto(url, wait_until="domcontentloaded")
        script = """
        () => Array.from(document.querySelectorAll('.liste-produits li, tr.ligne-panier')).map(n => {
            const t = (s) => { const e = n.querySelector(s); return e ? e.textContent.trim() : null; };
            const q = n.querySelector('input[type=text], input[type=number]');
            return {
                ref: n.getAttribute('data-ref') || n.id || (t('.libelle') || '').slice(0, 60),
                label: t('.libelle') || n.textContent.trim().slice(0, 140),
                qty: q ? q.value : (t('.quantite') || '1'),
                price: t('.prix') || ''
            };
        })
        """
        try:
            raw = page.evaluate(script)
        except Exception as exc:
            raise DriveError(f"lecture du panier impossible : {exc}") from exc
        lines: list[CartLine] = []
        for entry in raw or []:
            label = (entry.get("label") or "").strip()
            if not label:
                continue
            lines.append(
                CartLine(
                    ref=str(entry.get("ref") or label)[:80],
                    label=label,
                    quantity=_int(entry.get("qty")),
                    price_eur=_price(entry.get("price")),
                )
            )
        return lines

    def _add(self, product: DriveProduct, quantity: int) -> None:
        page = self.page()
        if product.url and not page.url.startswith(product.url.split("?")[0]):
            page.goto(product.url, wait_until="domcontentloaded")
        # On cible par le libellé visible : les clics par référence d'élément
        # échouent silencieusement (§7), et les coordonnées sont pires.
        selector = f'li:has-text("{product.label[:40]}") button:has-text("Ajouter")'
        for _ in range(max(1, quantity)):
            try:
                page.click(selector, timeout=15_000)
                page.wait_for_timeout(800)
            except Exception as exc:
                raise DriveError(f"clic d'ajout impossible sur {product.label!r} : {exc}") from exc

    def cart_remove(self, line: CartLine) -> None:
        """Suppression : une modale « Confirmation suppression de produit » suit
        chaque clic et doit être validée, sinon rien ne se passe."""
        page = self.page()
        page.goto(self.store.cart_url(), wait_until="domcontentloaded")
        page.click(f'li:has-text("{line.label[:40]}") a:has-text("Supprimer")', timeout=15_000)
        for confirm in ("Confirmer", "Oui", "Valider"):
            try:
                page.click(f'button:has-text("{confirm}")', timeout=3_000)
                break
            except Exception:
                continue
        page.wait_for_timeout(800)


def _price(raw: str | None) -> float | None:
    if not raw:
        return None
    match = re.search(r"(\d{1,4})[.,](\d{2})", raw)
    return float(f"{match.group(1)}.{match.group(2)}") if match else None


def _int(raw: str | None) -> int:
    if not raw:
        return 1
    match = re.search(r"\d+", str(raw))
    return int(match.group()) if match else 1


def parse_search_xhr(payload: str | dict) -> list[DriveProduct]:
    """Lecture d'une réponse JSON de recherche, quand le XHR répond.

    C'est la voie recommandée par le §7 : du JSON, stable et testable. La forme
    exacte varie selon les endpoints ; on accepte les clés les plus courantes
    et on ignore le reste plutôt que de casser.
    """
    data = json.loads(payload) if isinstance(payload, str) else payload
    candidates = None
    for key in ("produits", "products", "items", "results", "listeProduits"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, list):
            candidates = value
            break
    if candidates is None:
        return []

    products: list[DriveProduct] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        label = entry.get("libelle") or entry.get("label") or entry.get("name")
        if not label:
            continue
        price = entry.get("prix") or entry.get("price") or entry.get("prixUnitaire")
        try:
            price = float(str(price).replace(",", ".")) if price is not None else None
        except ValueError:
            price = None
        products.append(
            DriveProduct(
                ref=str(entry.get("ref") or entry.get("id") or label)[:80],
                label=str(label),
                price_eur=price,
                available=bool(entry.get("disponible", entry.get("available", True))),
                raw=entry,
            )
        )
    return products

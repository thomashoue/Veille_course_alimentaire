"""Capture de ce qu'un drive renvoie réellement, pour caler les sélecteurs.

Le problème que ça résout : les sélecteurs DOM et les endpoints XHR ont été
écrits d'après la spec, sans avoir jamais vu les vraies pages. Cette commande
tourne sur le poste de l'utilisateur, session ouverte, et enregistre :

  * le HTML de la page de recherche,
  * toutes les réponses JSON échangées pendant le chargement (les XHR — la voie
    recommandée par le §7),
  * une copie d'écran,
  * ce que les sélecteurs actuels arrivent à extraire, ou non.

Ces fichiers deviennent des fixtures de test : une fois qu'ils existent, les
sélecteurs se corrigent et se testent hors ligne, pour de bon.

⚠ Confidentialité : une page chargée en session connectée contient votre nom,
votre adresse de retrait et votre numéro de carte fidélité. Tout ce qui est
écrit ici passe par une passe de masquage (voir :func:`redact`), et AUCUN
en-tête ni cookie n'est enregistré. Relisez quand même avant de partager.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from .session import BrowserSession

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 400_000

# Motifs masqués avant écriture sur le disque.
_REDACTIONS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[email masqué]"),
    (re.compile(r"\b0[1-9](?:[ .-]?\d{2}){4}\b"), "[téléphone masqué]"),
    (re.compile(r"\b\d{9,}\b"), "[numéro masqué]"),
    (re.compile(r"(?i)\b(?:carte|fidelit[eé]|loyalty)[\"'\s:=]+([\w-]{6,})"), "carte [masquée]"),
]


def redact(text: str) -> str:
    """Masque ce qui identifie une personne. Volontairement large."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _looks_interesting(url: str, content_type: str) -> bool:
    """Ne garde que les échanges susceptibles de porter des produits."""
    if "json" not in (content_type or "").lower():
        return False
    needles = ("recherche", "search", "produit", "product", "panier", "cart", "api")
    return any(needle in url.lower() for needle in needles)


def capture_search(
    store,
    query: str,
    out_dir: Path | str,
    *,
    headless: bool = False,
    banner: str | None = None,
    include_cart: bool = False,
) -> Path:
    """Ouvre la recherche du drive et enregistre tout ce qui peut servir."""
    banner = banner or store.banner
    directory = Path(out_dir) / f"{store.id}-{datetime.now():%Y%m%d-%H%M%S}"
    directory.mkdir(parents=True, exist_ok=True)

    session = BrowserSession(banner, headless=headless)
    session.start()
    page = session.page()

    exchanges: list[dict] = []

    def on_response(response):
        try:
            content_type = response.headers.get("content-type", "")
            if not _looks_interesting(response.url, content_type):
                return
            body = response.text()
            exchanges.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "method": response.request.method,
                    # Le corps de la requête aide à rejouer l'appel ; les
                    # en-têtes ne sont volontairement PAS enregistrés (cookies).
                    "request_body": (response.request.post_data or "")[:4000],
                    "body": body[:MAX_BODY_BYTES],
                    "truncated": len(body) > MAX_BODY_BYTES,
                }
            )
        except Exception as exc:                       # une réponse illisible n'arrête rien
            log.debug("réponse ignorée (%s) : %s", response.url, exc)

    page.on("response", on_response)

    url = store.search_url(query)
    log.info("capture de %s", url)
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(2500)                        # le site est lent (§7)

    (directory / "search.html").write_text(redact(page.content()), encoding="utf-8")
    (directory / "xhr.json").write_text(
        redact(json.dumps(exchanges, ensure_ascii=False, indent=2)), encoding="utf-8"
    )
    try:
        page.screenshot(path=str(directory / "search.png"), full_page=False)
    except Exception as exc:
        log.warning("copie d'écran impossible : %s", exc)

    diagnostic = _diagnose(store, page, exchanges, query)
    (directory / "diagnostic.json").write_text(
        json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if include_cart and store.cart_url():
        exchanges.clear()
        page.goto(store.cart_url(), wait_until="networkidle")
        page.wait_for_timeout(2000)
        (directory / "cart.html").write_text(redact(page.content()), encoding="utf-8")
        (directory / "cart-xhr.json").write_text(
            redact(json.dumps(exchanges, ensure_ascii=False, indent=2)), encoding="utf-8"
        )

    session.close()
    return directory


def _diagnose(store, page, exchanges: list[dict], query: str) -> dict:
    """Ce que les sélecteurs actuels donnent — c'est le vrai résultat du test."""
    from . import get_client

    result = {
        "store": store.id,
        "query": query,
        "url": page.url,
        "page_size": len(page.content()),
        "xhr_json_captured": len(exchanges),
        "xhr_urls": [e["url"] for e in exchanges][:20],
    }
    try:
        client = get_client(store.banner, store)
        client._page = page                            # on réutilise la page ouverte
        client.session = type("_", (), {"close": lambda self=None: None})()
        products = client._products_from_dom(page) if hasattr(client, "_products_from_dom") else []
        result["products_found_by_current_selectors"] = len(products)
        result["sample"] = [
            {"label": p.label[:100], "price": p.price_eur} for p in products[:5]
        ]
    except Exception as exc:
        result["selector_error"] = str(exc)
    result["verdict"] = (
        "les sélecteurs actuels fonctionnent"
        if result.get("products_found_by_current_selectors")
        else "les sélecteurs actuels ne trouvent RIEN — à corriger avec cette capture"
    )
    return result

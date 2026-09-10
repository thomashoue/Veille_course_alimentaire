"""Page « Menu de la semaine » servie localement, avec validation qui écrit.

Le jeudi, la famille ouvre la page sur une tablette (même wifi que le PC qui
fait la veille), choisit les 7 dîners et clique « Valider ». La validation ne
donne pas une commande à recopier : elle POST vers ce serveur, qui écrit
directement l'état sur le PC —

  * `data/menu_courant.json`  : le menu validé + la liste de courses (lu le
    vendredi par la veille prix) ;
  * l'historique des semaines  (évite de reproposer les mêmes plats) ;
  * l'inventaire                (décrémente le stock des ingrédients cuisinés).

La même page fonctionne aussi en fichier statique (Artifact) : sans serveur,
le clic Valider retombe sur le mail récapitulatif.
"""

from __future__ import annotations

import json
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import DATA_DIR, Config
from .menu import record_week, shopping_list

# Marqueurs qui délimitent le bloc de données injecté dans la page. Le script
# de build et le serveur remplacent tout ce qui se trouve entre eux.
DATA_OPEN = '<script id="menu-data" type="application/json">'
DATA_CLOSE = "</script>"


def _template_path() -> Path:
    return Path(__file__).resolve().parent.parent / "web" / "menu-semaine.html"


# --------------------------------------------------------------------------- #
def build_menu_data(config: Config) -> dict:
    """Données que la page consomme : recettes, catégories, destinataires."""
    recipes = []
    for r in config.recipes.values():
        recipes.append(
            {
                "id": r.id,
                "name": r.name,
                "tags": list(r.tags),
                "proteine": r.proteine,
                "ingredients": [
                    {
                        "label": ing.label,
                        "qty": ing.qty,
                        "unit": ing.unit,
                        "basket_item": ing.basket_item,
                        "category": ing.category,
                    }
                    for ing in r.ingredients
                ],
            }
        )
    item_categories = {name: item.category for name, item in config.items.items()}
    recipients = (config.sources.get("report", {}) or {}).get("recipients", [])
    return {
        "servings_base": config.servings_base,
        "recipes": recipes,
        "item_categories": item_categories,
        "recipients": list(recipients),
    }


def render_page(config: Config, template_html: str | None = None) -> str:
    """Injecte des données fraîches (config) dans le gabarit de la page."""
    if template_html is None:
        template_html = _template_path().read_text(encoding="utf-8")
    payload = json.dumps(build_menu_data(config), ensure_ascii=False)
    start = template_html.find(DATA_OPEN)
    if start == -1:
        return template_html
    body = start + len(DATA_OPEN)
    end = template_html.find(DATA_CLOSE, body)
    if end == -1:
        return template_html
    return template_html[:body] + payload + template_html[end:]


# --------------------------------------------------------------------------- #
def _menu_courant_path() -> Path:
    return DATA_DIR / "menu_courant.json"


def save_menu_choice(
    config: Config,
    ids: list[str],
    servings: int | None = None,
    cook: bool = True,
) -> dict:
    """Enregistre le menu validé : fichier courant, historique, stock.

    Renvoie un résumé (menu + liste) pour l'accusé de réception de la page.
    Lève ``ValueError`` si un identifiant de recette est inconnu.
    """
    inconnus = [i for i in ids if i not in config.recipes]
    if inconnus:
        raise ValueError(f"Recette(s) inconnue(s) : {', '.join(inconnus)}")
    if not ids:
        raise ValueError("Aucune recette sélectionnée.")

    week = [config.recipe(i) for i in ids]
    servings = int(servings or config.servings_base)

    stock = None
    from .inventory import Inventory, consume_menu

    inv = Inventory()
    stock = inv.current()
    menu = shopping_list(config, week, servings=servings, stock=stock)

    def _lines(lines):
        return [
            {"label": l.label, "qty": l.qty, "unit": l.unit, "category": l.category}
            for l in lines
        ]

    payload = {
        "date": date.today().isoformat(),
        "servings": servings,
        "menu": [{"jour": j, "id": r.id, "nom": r.name} for j, r in menu.week],
        "to_buy": _lines(menu.to_buy),
        "fresh": _lines(menu.fresh),
        "pantry": _lines(menu.pantry),
    }

    path = _menu_courant_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if cook:
        consume_menu(inv, config, week, servings=servings)
        inv.save()
    record_week([r.id for r in week])
    return payload


# --------------------------------------------------------------------------- #
def serve(config: Config, host: str = "0.0.0.0", port: int = 8000) -> None:
    """Sert la page et traite POST /api/validate (validation → écriture)."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (API imposée par BaseHTTPRequestHandler)
            if self.path in ("/", "/index.html", "/menu-semaine.html"):
                html = render_page(config).encode("utf-8")
                self._send(200, html, "text/html; charset=utf-8")
            else:
                self._send(404, b"Not found", "text/plain; charset=utf-8")

        def do_POST(self):  # noqa: N802
            if self.path != "/api/validate":
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
                ids = data.get("ids") or []
                if isinstance(ids, str):
                    ids = [i.strip() for i in ids.replace(" ", ",").split(",") if i.strip()]
                summary = save_menu_choice(config, ids, servings=data.get("servings"))
                body = json.dumps({"ok": True, "saved": summary}, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except ValueError as exc:
                body = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self._send(400, body, "application/json; charset=utf-8")
            except Exception as exc:  # pragma: no cover - garde-fou serveur
                body = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self._send(500, body, "application/json; charset=utf-8")

        def log_message(self, *args):  # silence le log par requête
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    shown = "localhost" if host in ("0.0.0.0", "") else host
    print(f"Menu de la semaine servi sur http://{shown}:{port}/")
    if host in ("0.0.0.0", ""):
        print("Depuis la tablette (même wifi) : http://<IP-du-PC>:%d/" % port)
    print("Ctrl+C pour arrêter.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServeur arrêté.")
    finally:
        httpd.server_close()

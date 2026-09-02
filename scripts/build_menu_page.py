#!/usr/bin/env python3
"""Rafraîchit les données figées dans web/menu-semaine.html depuis la config.

La page embarque un instantané des recettes (bloc <script id="menu-data">) pour
fonctionner aussi en fichier statique (Artifact). Ce script réinjecte cet
instantané à partir de config/recipes.yaml + config/basket.yaml + sources.yaml,
pour que la version du dépôt ne dérive pas de la config. Servie par
`python -m src.cli menu --serve`, la page reçoit de toute façon des données
fraîches à chaque requête ; ce script ne sert qu'à garder le fichier commité à
jour.

Usage :  python scripts/build_menu_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.webmenu import render_page  # noqa: E402


def main() -> int:
    page = ROOT / "web" / "menu-semaine.html"
    if not page.exists():
        print(f"Introuvable : {page}")
        return 1
    config = load_config()
    refreshed = render_page(config, page.read_text(encoding="utf-8"))
    page.write_text(refreshed, encoding="utf-8")
    print(f"Instantané des recettes réinjecté dans {page.relative_to(ROOT)} "
          f"({len(config.recipes)} recettes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

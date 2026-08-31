"""Session navigateur persistante — aucun identifiant stocké.

Modèle retenu (§7) : l'humain se connecte lui-même une fois, la session
persiste par cookies pendant des semaines dans un profil Chromium local.

Ce module ne saisit JAMAIS de mot de passe, ne lit aucun ``.env`` de
credentials et n'en crée pas. C'est une contrainte fonctionnelle, pas une
commodité : la demande d'automatiser la connexion a été explicitement écartée.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .base import DriveUnavailable

log = logging.getLogger(__name__)

PROFILE_ROOT = Path(os.environ.get("VEILLE_PROFILE_DIR", Path.home() / ".veille-courses" / "profiles"))

# Le sous-domaine du drive doit être autorisé séparément : se connecter sur
# www.leclercdrive.fr ne propage rien vers fd7-courses.leclercdrive.fr tant
# qu'on n'a pas cliqué « Commencer mes courses ».
BANNER_DOMAINS = {
    "leclerc": ["leclercdrive.fr", "fd7-courses.leclercdrive.fr"],
    "intermarche": ["intermarche.com"],
    "u": ["coursesu.com"],
}


def profile_dir(banner: str) -> Path:
    path = PROFILE_ROOT / banner
    path.mkdir(parents=True, exist_ok=True)
    return path


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - dépend de l'installation
        raise DriveUnavailable(
            "Playwright n'est pas installé. `pip install playwright` puis "
            "`playwright install chromium` — ou utilisez --offline."
        ) from exc
    return sync_playwright


class BrowserSession:
    """Contexte Chromium persistant, ouvert sur un profil déjà authentifié."""

    def __init__(self, banner: str, headless: bool = True, slow_mo_ms: int = 0):
        self.banner = banner
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self._playwright = None
        self.context = None

    def start(self):
        sync_playwright = _playwright()
        self._playwright = sync_playwright().start()
        launch_kwargs = {
            "user_data_dir": str(profile_dir(self.banner)),
            "headless": self.headless,
            "slow_mo": self.slow_mo_ms,
            "locale": "fr-FR",
            "viewport": {"width": 1400, "height": 1000},
        }
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")
        if executable:
            launch_kwargs["executable_path"] = executable
        self.context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        # Le site est lent (§7) : des timeouts courts produisent des faux
        # négatifs qu'on prendrait pour « produit absent du drive ».
        self.context.set_default_timeout(45_000)
        self.context.set_default_navigation_timeout(60_000)
        return self.context

    def page(self):
        if self.context is None:
            self.start()
        pages = self.context.pages
        return pages[0] if pages else self.context.new_page()

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
            self.context = None
            self._playwright = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def interactive_login(banner: str, url: str | None = None) -> None:
    """Ouvre un navigateur visible pour que l'HUMAIN se connecte lui-même.

    Rien n'est saisi par le programme. À la fermeture, les cookies restent dans
    le profil et les runs suivants tournent en headless.
    """
    landing = {
        "leclerc": "https://www.leclercdrive.fr/",
        "intermarche": "https://www.intermarche.com/",
        "u": "https://www.coursesu.com/",
    }
    session = BrowserSession(banner, headless=False)
    session.start()
    page = session.page()
    page.goto(url or landing.get(banner, "about:blank"))
    print(
        f"\nConnectez-vous à {banner} dans la fenêtre ouverte.\n"
        "Pour Leclerc : après la connexion, cliquez « Commencer mes courses » — "
        "sans ce clic la session n'est PAS propagée vers fd7-courses.leclercdrive.fr.\n"
        "Fermez ensuite la fenêtre : les cookies restent dans "
        f"{profile_dir(banner)}.\n"
        "Aucun identifiant n'est enregistré par ce programme.\n"
    )
    try:
        page.wait_for_event("close", timeout=0)
    except Exception:
        pass
    finally:
        session.close()

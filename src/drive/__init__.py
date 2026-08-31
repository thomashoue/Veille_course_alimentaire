"""Clients drive — la seule source de vérité (constat C1).

Ordre de robustesse retenu (§7) :
  1. appels XHR du drive une fois la session ouverte : c'est du JSON, stable ;
  2. à défaut Playwright sur un profil persistant authentifié À LA MAIN, avec
     vérification de l'état du panier après CHAQUE mutation ;
  3. le pilotage par coordonnées d'écran est considéré comme non fiable et
     n'est pas implémenté.

Contraintes posées par Thomas et respectées ici :
  * le code remplit le panier et s'arrête là — créneau et paiement restent humains ;
  * aucun identifiant n'est stocké, nulle part, jamais.
"""

from .base import CartLine, DriveClient, DriveError, DriveProduct, FixtureDriveClient
from .verify import verify_in_drive

__all__ = [
    "CartLine",
    "DriveClient",
    "DriveError",
    "DriveProduct",
    "FixtureDriveClient",
    "verify_in_drive",
    "get_client",
]


def get_client(banner: str, store, **kwargs):
    """Instancie le client de l'enseigne. Import paresseux : Playwright est optionnel."""
    if banner == "leclerc":
        from .leclerc import LeclercDrive

        return LeclercDrive(store, **kwargs)
    if banner == "intermarche":
        from .intermarche import IntermarcheDrive

        return IntermarcheDrive(store, **kwargs)
    if banner == "u":
        from .coursesu import CoursesUDrive

        return CoursesUDrive(store, **kwargs)
    raise DriveError(f"pas de client drive pour l'enseigne {banner!r}")

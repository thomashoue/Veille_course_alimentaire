"""Collecteurs de prix (agrégateurs de prospectus).

Rappel du constat C1 : ce que produisent ces collecteurs n'est jamais une
offre, seulement une PISTE. Le flag ``verified_in_drive`` reste à False tant
que :mod:`src.drive` n'a pas retrouvé le produit dans le drive.
"""

from .promocatalogues import PromocataloguesCollector
from .vospromos import VosPromosCollector
from .action import ActionCollector

COLLECTORS = {
    "promocatalogues": PromocataloguesCollector,
    "vospromos": VosPromosCollector,
    "action": ActionCollector,
}

__all__ = ["COLLECTORS", "PromocataloguesCollector", "VosPromosCollector", "ActionCollector"]

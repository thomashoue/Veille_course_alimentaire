"""Fixtures partagées. Aucun test ne touche le réseau : c'est la condition
pour que validate.py et normalize.py restent testables en toutes circonstances.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import get_config  # noqa: E402
from src.ledger import Ledger  # noqa: E402
from src.models import PriceObservation  # noqa: E402
from src.normalize import normalize  # noqa: E402


@pytest.fixture(scope="session")
def config():
    return get_config(ROOT / "config")


@pytest.fixture
def ledger():
    ledger = Ledger(":memory:")
    yield ledger
    ledger.close()


@pytest.fixture
def observe(config):
    """Construit une observation normalisée, comme le pipeline le ferait."""

    def _observe(**kwargs):
        kwargs.setdefault("store_id", "leclerc_pleumeleuc")
        kwargs.setdefault("verified_in_drive", True)
        pickup = kwargs.pop("pickup_date", None)
        obs = PriceObservation(**kwargs)
        normalize(obs, config, pickup_date=pickup)
        return obs

    return _observe

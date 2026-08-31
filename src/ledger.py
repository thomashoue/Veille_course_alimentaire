"""Historique des relevés et détection de « vrai » record — backlog P1.

Sert à ne pas re-signaler chaque semaine une offre médiocre comme une
trouvaille : un record doit battre le précédent d'une marge minimale.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config import DATA_DIR, Config
from .models import PriceObservation, Verdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id                  TEXT PRIMARY KEY,
    observed_at         TEXT NOT NULL,
    store_id            TEXT NOT NULL,
    banner              TEXT,
    basket_item_id      TEXT NOT NULL,
    product_label       TEXT NOT NULL,
    pack_size           REAL,
    pack_unit           TEXT,
    pack_count          INTEGER DEFAULT 1,
    price_eur           REAL NOT NULL,
    regular_price       REAL,
    unit_price          REAL,
    unit_price_unit     TEXT,
    effective_unit_price REAL,
    weight_basis        TEXT,
    mechanic            TEXT,
    loyalty_pct         REAL,
    source              TEXT,
    verified_in_drive   INTEGER NOT NULL DEFAULT 0,
    source_url          TEXT,
    attributes          TEXT,
    status              TEXT,
    rules               TEXT,
    notes               TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_item ON observations(basket_item_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_store ON observations(store_id, observed_at);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    n_observed  INTEGER DEFAULT 0,
    n_offers    INTEGER DEFAULT 0,
    summary     TEXT
);
"""


@dataclass
class RecordCheck:
    is_record: bool
    previous_best: float | None
    previous_store: str | None = None
    previous_date: str | None = None


class Ledger:
    """Accès SQLite. Un seul fichier, versionnable, lisible avec n'importe quoi."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DATA_DIR / "observations.sqlite"
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        with closing(self.conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def record(self, obs: PriceObservation, verdict: Verdict | None = None) -> None:
        """Enregistre une observation (idempotent sur son empreinte).

        On stocke AUSSI les pistes non vérifiées : elles servent à orienter la
        vérification en drive de la semaine suivante. C'est la lecture qui
        filtre, pas l'écriture.
        """
        row = {
            "id": obs.id,
            "observed_at": obs.observed_at.isoformat(),
            "store_id": obs.store_id,
            "banner": obs.banner,
            "basket_item_id": obs.basket_item_id,
            "product_label": obs.product_label,
            "pack_size": obs.pack_size,
            "pack_unit": obs.pack_unit,
            "pack_count": obs.pack_count,
            "price_eur": obs.price_eur,
            "regular_price": obs.regular_price,
            "unit_price": obs.unit_price,
            "unit_price_unit": obs.unit_price_unit,
            "effective_unit_price": obs.effective_unit_price,
            "weight_basis": obs.weight_basis,
            "mechanic": obs.mechanic,
            "loyalty_pct": obs.loyalty_pct,
            "source": obs.source,
            "verified_in_drive": int(obs.verified_in_drive),
            "source_url": obs.source_url,
            "attributes": json.dumps(obs.attributes, ensure_ascii=False),
            "status": verdict.status.value if verdict else None,
            "rules": ",".join(verdict.rules) if verdict else None,
            "notes": json.dumps(obs.notes, ensure_ascii=False),
        }
        columns = ", ".join(row)
        placeholders = ", ".join(f":{c}" for c in row)
        with closing(self.conn.cursor()) as cur:
            cur.execute(
                f"INSERT OR REPLACE INTO observations ({columns}) VALUES ({placeholders})", row
            )
        self.conn.commit()

    def record_all(self, pairs: list[tuple[PriceObservation, Verdict | None]]) -> int:
        for obs, verdict in pairs:
            self.record(obs, verdict)
        return len(pairs)

    # ------------------------------------------------------------------ #
    def best_price(
        self,
        basket_item_id: str,
        *,
        verified_only: bool = True,
        attribute_filter: tuple[str, str] | None = None,
        since_days: int | None = None,
        exclude_id: str | None = None,
    ) -> sqlite3.Row | None:
        """Meilleur prix normalisé jamais relevé pour un article."""
        query = [
            "SELECT * FROM observations",
            "WHERE basket_item_id = ? AND effective_unit_price IS NOT NULL",
        ]
        params: list[object] = [basket_item_id]
        if verified_only:
            query.append("AND verified_in_drive = 1")
        if since_days:
            cutoff = (datetime.now() - timedelta(days=since_days)).isoformat()
            query.append("AND observed_at >= ?")
            params.append(cutoff)
        if exclude_id:
            query.append("AND id != ?")
            params.append(exclude_id)
        query.append("ORDER BY effective_unit_price ASC LIMIT 20")

        with closing(self.conn.cursor()) as cur:
            rows = cur.execute(" ".join(query), params).fetchall()

        if attribute_filter:
            key, value = attribute_filter
            rows = [r for r in rows if json.loads(r["attributes"] or "{}").get(key) == value]
        return rows[0] if rows else None

    def check_record(self, obs: PriceObservation, config: Config) -> RecordCheck:
        """Un record, c'est battre le précédent de plus que la marge de bruit.

        Une baisse de 1 centime sur un prix au kilo n'est pas une trouvaille.
        """
        price = obs.best_unit_price
        if price is None or not obs.verified_in_drive:
            return RecordCheck(False, None)

        item = config.items.get(obs.basket_item_id)
        attribute_filter = None
        if item is not None and item.attribute_rules:
            key = next(iter(item.attribute_rules))
            value = obs.attributes.get(key)
            if value:
                attribute_filter = (key, value)

        previous = self.best_price(
            obs.basket_item_id, attribute_filter=attribute_filter, exclude_id=obs.id
        )
        if previous is None:
            return RecordCheck(True, None)

        margin = float(config.param("record_margin_pct", 2.0)) / 100.0
        best = float(previous["effective_unit_price"])
        return RecordCheck(
            is_record=price < best * (1.0 - margin),
            previous_best=best,
            previous_store=previous["store_id"],
            previous_date=previous["observed_at"][:10],
        )

    # ------------------------------------------------------------------ #
    def history(self, basket_item_id: str, limit: int = 50) -> list[sqlite3.Row]:
        with closing(self.conn.cursor()) as cur:
            return cur.execute(
                "SELECT * FROM observations WHERE basket_item_id = ? "
                "AND effective_unit_price IS NOT NULL "
                "ORDER BY observed_at DESC LIMIT ?",
                (basket_item_id, limit),
            ).fetchall()

    def trend(self, basket_item_id: str, weeks: int = 12) -> dict[str, float] | None:
        """Tendance grossière : moyenne récente vs moyenne de la période.

        Sert à écrire « tendance baissière, pas d'urgence » sans le deviner.
        """
        cutoff = (datetime.now() - timedelta(weeks=weeks)).isoformat()
        recent_cutoff = (datetime.now() - timedelta(weeks=max(1, weeks // 4))).isoformat()
        with closing(self.conn.cursor()) as cur:
            overall = cur.execute(
                "SELECT AVG(effective_unit_price) AS avg, COUNT(*) AS n FROM observations "
                "WHERE basket_item_id = ? AND observed_at >= ? "
                "AND effective_unit_price IS NOT NULL AND verified_in_drive = 1",
                (basket_item_id, cutoff),
            ).fetchone()
            recent = cur.execute(
                "SELECT AVG(effective_unit_price) AS avg, COUNT(*) AS n FROM observations "
                "WHERE basket_item_id = ? AND observed_at >= ? "
                "AND effective_unit_price IS NOT NULL AND verified_in_drive = 1",
                (basket_item_id, recent_cutoff),
            ).fetchone()

        if not overall or not overall["n"] or not recent or not recent["n"]:
            return None
        delta_pct = (recent["avg"] - overall["avg"]) / overall["avg"] * 100.0
        return {
            "average": float(overall["avg"]),
            "recent_average": float(recent["avg"]),
            "delta_pct": float(delta_pct),
            "n": int(overall["n"]),
        }

    # ------------------------------------------------------------------ #
    def start_run(self) -> int:
        with closing(self.conn.cursor()) as cur:
            cur.execute("INSERT INTO runs (started_at) VALUES (?)", (datetime.now().isoformat(),))
            self.conn.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, n_observed: int, n_offers: int, summary: str = "") -> None:
        with closing(self.conn.cursor()) as cur:
            cur.execute(
                "UPDATE runs SET finished_at = ?, n_observed = ?, n_offers = ?, summary = ? "
                "WHERE id = ?",
                (datetime.now().isoformat(), n_observed, n_offers, summary, run_id),
            )
        self.conn.commit()

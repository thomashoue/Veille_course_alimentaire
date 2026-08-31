"""Anti-pièges P1…P7 — §5 de la spec.

C'est ici qu'est la valeur du projet : les agrégateurs publient des prix faux
de façon *systématique et prévisible* (constat C2), donc mécanisable.

Chaque règle porte le contre-exemple réel qui l'a motivée. Le module est pur
(aucun réseau, aucune I/O) : il est intégralement testable hors ligne.
"""

from __future__ import annotations

import re
from datetime import date

from .config import Config
from .models import (
    FLAG,
    OK,
    REJECT,
    BasketItem,
    Grade,
    PriceObservation,
    Status,
    Verdict,
)
from .normalize import loyalty_applies
from .units import comparable_units

# Un ratio prix habituel / prix promo à ce point exact ne peut pas être une
# remise : c'est un lot ou un « 2ᵉ à −50 % » présenté comme le prix unitaire.
RATIO_DOUBLE = 2.0
RATIO_DOUBLE_TOLERANCE = 0.02
RATIO_ABSURD = 2.4


# --------------------------------------------------------------------------- #
# Contraintes dures (DSL minimal de basket.yaml)
# --------------------------------------------------------------------------- #
_CONSTRAINT_RE = re.compile(
    r"^\s*(?P<field>\w+)\s+(?P<op>not\s+in|in|==|!=|<=|>=|<|>)\s+(?P<value>.+?)\s*$",
    re.I,
)


class ConstraintError(ValueError):
    """Contrainte mal écrite dans la config — c'est un bug de config, pas une donnée."""


def _parse_values(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    return [v.strip().strip("'\"") for v in raw.split(",") if v.strip()]


def check_constraint(expression: str, attributes: dict[str, str]) -> Verdict:
    """Évalue une contrainte comme ``type in (silice, agglo_charbon)``.

    Attribut inconnu ⇒ FLAG, jamais OK : on ne laisse pas passer un produit
    « peut-être conforme ».
    """
    match = _CONSTRAINT_RE.match(expression or "")
    if not match:
        raise ConstraintError(f"contrainte illisible : {expression!r}")

    field = match.group("field")
    operator = re.sub(r"\s+", " ", match.group("op").lower())
    values = _parse_values(match.group("value"))
    actual = attributes.get(field)

    if actual is None:
        return FLAG(f"contrainte « {expression} » invérifiable : {field} inconnu", "C-HARD")

    if operator == "in":
        ok = actual in values
    elif operator == "not in":
        ok = actual not in values
    elif operator == "==":
        ok = actual == values[0]
    elif operator == "!=":
        ok = actual != values[0]
    else:
        try:
            ok = {
                "<": lambda a, b: a < b,
                "<=": lambda a, b: a <= b,
                ">": lambda a, b: a > b,
                ">=": lambda a, b: a >= b,
            }[operator](float(actual), float(values[0]))
        except (TypeError, ValueError) as exc:
            raise ConstraintError(f"comparaison numérique impossible : {expression!r}") from exc

    if ok:
        return OK()
    return REJECT(
        f"non conforme : {field}={actual}, attendu « {expression} »", "C-HARD"
    )


def check_hard_constraints(obs: PriceObservation, item: BasketItem) -> Verdict:
    """La conformité produit précède l'optimisation prix (§2.2).

    Une litière minérale à 0,30 €/L n'est pas une affaire, c'est un hors-sujet.
    """
    verdict = OK()
    for expression in item.hard_constraints or []:
        verdict = verdict.merge(check_constraint(expression, obs.attributes))
    return verdict


# --------------------------------------------------------------------------- #
# Règles P1…P7
# --------------------------------------------------------------------------- #
def p1_exact_double(obs: PriceObservation) -> Verdict:
    """Ratio exactement 2,00 → lot ou « 2ᵉ à −50 % », jamais une remise de moitié."""
    if not obs.regular_price or not obs.price_eur:
        return OK()
    ratio = obs.regular_price / obs.price_eur
    if abs(ratio - RATIO_DOUBLE) < RATIO_DOUBLE_TOLERANCE:
        return REJECT(
            f"prix habituel au double exact (x{ratio:.2f}) = mécanique 2ᵉ article", "P1"
        )
    return OK()


def p2_absurd_ratio(obs: PriceObservation) -> Verdict:
    """Ratio > 2,4 → donnée aberrante : le « promo » est en fait le prix normal."""
    if not obs.regular_price or not obs.price_eur:
        return OK()
    ratio = obs.regular_price / obs.price_eur
    if ratio > RATIO_ABSURD:
        return REJECT(f"ratio aberrant (x{ratio:.2f}), agrégateur peu fiable", "P2")
    return OK()


def p3_mechanic_mean(obs: PriceObservation) -> Verdict:
    """Mécanique « Nᵉ à −X % » : le prix pertinent est la moyenne sur N.

    Vécu : 4,43 € le 1er sac + 3,10 € le 2ᵉ = 0,377 €/L, pas 0,31 €/L.
    Le calcul est fait dans :mod:`src.normalize` ; ici on vérifie qu'il l'a été.
    """
    if not obs.mechanic or not obs.mechanic.startswith("second_"):
        return OK()
    if obs.effective_unit_price is None:
        return FLAG("mécanique 2ᵉ article non ramenée à la moyenne", "P3")
    return OK(
        "P3",
        f"prix moyen sur {obs.required_qty} unités (mécanique {obs.mechanic})",
    )


def p4_missing_pack(obs: PriceObservation) -> Verdict:
    """Grammage absent → interdiction de calculer un €/kg."""
    if obs.pack_size is None:
        return FLAG("format non précisé — ne pas annoncer de prix au kilo", "P4")
    return OK()


def p5_weight_basis(obs: PriceObservation, item: BasketItem, config: Config) -> Verdict:
    """Poids brut vs net égoutté : incomparable sans conversion.

    Leclerc annonce en brut, Intermarché en net égoutté.
    """
    needs_basis = item.weight_basis_required or obs.category == "conserve"
    if not needs_basis:
        return OK()
    if obs.weight_basis is None:
        return FLAG("base de poids inconnue (brut ou net égoutté ?)", "P5")
    ratios = config.param("drained_ratios", {}) or {}
    if obs.weight_basis == "brut" and obs.category not in ratios:
        return FLAG("poids brut sans ratio d'égouttage connu — non comparable", "P5")
    return OK()


def p6_loyalty_window(obs: PriceObservation, pickup_date: date | None) -> Verdict:
    """L'avantage carte ne s'applique que si la date de RETRAIT est dans la fenêtre."""
    if not obs.loyalty_valid_until:
        return OK()
    if pickup_date and pickup_date > obs.loyalty_valid_until:
        obs.loyalty_pct = 0.0
        return FLAG(
            f"avantage carte expiré au retrait ({obs.loyalty_valid_until:%d/%m}) — non compté",
            "P6",
        )
    if obs.loyalty_pct and not loyalty_applies(obs, pickup_date):
        return FLAG("avantage carte hors fenêtre", "P6")
    return OK()


def p7_small_format(obs: PriceObservation, item: BasketItem, config: Config) -> Verdict:
    """Petits formats en promo : convertir au kilo AVANT de conclure.

    Vécu : 125 g à −30 % = 11,52 €/kg contre un 500 g plein tarif à 6,78 €/kg.
    On ne rejette pas : on compare sur le normalisé, ce que fait :func:`grade`.
    La règle sert à signaler le piège dans le rapport.
    """
    price = obs.best_unit_price
    if price is None:
        return OK()
    thresholds = _threshold_for_unit(
        config.threshold(item.id, obs.attributes), obs.unit_price_unit or item.unit
    )
    good = (thresholds or {}).get("good")
    if good and obs.mechanic and price > good:
        return FLAG(
            f"promo trompeuse : {price:.2f} €/{item.base_unit} malgré la remise "
            f"(seuil {good:.2f})",
            "P7",
        )
    return OK()


def p8_worth_detour(saving_eur: float, detour_km: float, config: Config) -> tuple[bool, float]:
    """Coût du détour : une économie ne vaut que si elle dépasse le carburant.

    Règle calibrée : ~2,50 € d'économie pour ~25 km ⇒ 0,10 €/km.
    Renvoie ``(ça vaut le coup, gain net)``.
    """
    cost_per_km = float(config.param("cost_per_km", 0.10))
    minimum = float(config.param("min_net_gain_eur", 0.0))
    detour_cost = float(detour_km) * cost_per_km
    net = saving_eur - detour_cost
    return (net >= minimum, net)


def check_comparable_unit(obs: PriceObservation, item: BasketItem, config: Config) -> Verdict:
    """Un seuil en €/L ne juge pas un prix en €/kg (constat C3).

    Sans cette garde, une lessive relevée au litre serait comparée au seuil
    à la dose et déclarée « bonne affaire » sur un malentendu.
    """
    thresholds = config.thresholds.get(item.id)
    if not thresholds or obs.unit_price_unit is None:
        return OK()
    if _threshold_for_unit(thresholds, obs.unit_price_unit) is None:
        return FLAG(
            f"seuil exprimé en €/{thresholds.get('unit')} mais relevé en "
            f"€/{obs.unit_price_unit} — non comparable",
            "C3",
        )
    return OK()


def _threshold_for_unit(thresholds: dict, unit: str) -> dict | None:
    """Choisit le jeu de seuils dont l'unité correspond au relevé.

    ``alt`` permet à un même article d'avoir deux références (la lessive se
    juge à la dose, ou à défaut au litre).
    """
    declared = thresholds.get("unit")
    if not declared or comparable_units(declared, unit):
        return thresholds
    alt = thresholds.get("alt")
    if alt and alt.get("unit") and comparable_units(alt["unit"], unit):
        merged = {k: v for k, v in thresholds.items() if k not in {"good", "stock", "ceiling", "unit", "alt"}}
        merged.update(alt)
        return merged
    return None


# --------------------------------------------------------------------------- #
# Filtres structurants
# --------------------------------------------------------------------------- #
def check_suspect(obs: PriceObservation) -> Verdict:
    """Une incohérence relevée à la lecture disqualifie l'offre, pas la piste."""
    if obs.suspect_reason:
        return FLAG(obs.suspect_reason, "C-CHECK")
    return OK()


def check_excluded_store(obs: PriceObservation, config: Config) -> Verdict:
    """Carrefour / Auchan : exclusion dure, en entrée du pipeline (§2.1)."""
    if config.is_excluded(obs.store_id) or config.is_excluded((obs.banner or "").lower()):
        return REJECT("enseigne exclue (Carrefour / Auchan)", "X-BANNER")
    return OK()


def check_drive_verification(obs: PriceObservation) -> Verdict:
    """C1 : le catalogue n'est pas l'assortiment du drive.

    Non vérifiée en drive ⇒ piste, jamais offre. Une enseigne sans drive n'a
    rien à vérifier : son prix prospectus est le prix magasin, elle sort en
    liste papier.
    """
    if not obs.available:
        return REJECT("indisponible en drive", "C1")
    if not obs.requires_drive_verification:
        return OK("C1", "enseigne sans drive — achat en magasin, liste papier")
    if not obs.verified_in_drive:
        return FLAG("non vérifiée en drive — piste, pas une offre", "C1")
    return OK()


# --------------------------------------------------------------------------- #
# Verdict complet
# --------------------------------------------------------------------------- #
def validate(
    obs: PriceObservation,
    config: Config,
    item: BasketItem | None = None,
    pickup_date: date | None = None,
) -> Verdict:
    """Applique toutes les règles. L'ordre est celui de la sévérité décroissante."""
    item = item or config.items.get(obs.basket_item_id)
    if item is None:
        return REJECT(f"article inconnu au panier : {obs.basket_item_id}", "C-ITEM")

    verdict = OK()
    for rule_verdict in (
        check_excluded_store(obs, config),
        check_hard_constraints(obs, item),
        p1_exact_double(obs),
        p2_absurd_ratio(obs),
        p3_mechanic_mean(obs),
        p4_missing_pack(obs),
        p5_weight_basis(obs, item, config),
        p6_loyalty_window(obs, pickup_date),
        p7_small_format(obs, item, config),
        check_comparable_unit(obs, item, config),
        check_suspect(obs),
        check_drive_verification(obs),
    ):
        verdict = verdict.merge(rule_verdict)
        if verdict.rejected:
            break
    return verdict


# --------------------------------------------------------------------------- #
# Notation par rapport aux seuils (§4)
# --------------------------------------------------------------------------- #
def grade(obs: PriceObservation, config: Config, item: BasketItem | None = None) -> Grade:
    """Situe un prix normalisé par rapport aux seuils calibrés."""
    item = item or config.items[obs.basket_item_id]
    thresholds = config.threshold(item.id, obs.attributes)
    price = obs.best_unit_price
    if price is None or not thresholds:
        return Grade.NORMAL
    thresholds = _threshold_for_unit(thresholds, obs.unit_price_unit or item.unit)
    if thresholds is None:
        # Unité incomparable : on ne note pas plutôt que de noter faux.
        return Grade.NORMAL

    ceiling = thresholds.get("ceiling")
    if ceiling and price > float(ceiling):
        return Grade.TOO_HIGH

    stock = thresholds.get("stock")
    if stock and price < float(stock):
        return Grade.STOCK

    good = thresholds.get("good")
    if good and price <= float(good):
        # Un prix dans la fourchette « normale » déclarée n'est pas une promo :
        # le Comté à 19 €/kg est un prix courant, pas une trouvaille.
        normal_range = thresholds.get("normal_range")
        if normal_range and float(normal_range[0]) <= price <= float(normal_range[1]):
            return Grade.NORMAL
        return Grade.GOOD

    return Grade.NORMAL


def saving_vs_threshold(
    obs: PriceObservation, config: Config, item: BasketItem | None = None
) -> float:
    """Économie estimée sur la quantité du run, par rapport au seuil « bon ».

    Faute d'un prix de référence historique, le seuil sert de référence : c'est
    délibérément conservateur, on ne gonfle pas les économies annoncées.
    """
    item = item or config.items[obs.basket_item_id]
    thresholds = _threshold_for_unit(
        config.threshold(item.id, obs.attributes), obs.unit_price_unit or item.unit
    )
    price = obs.best_unit_price
    reference = (thresholds or {}).get("good")
    if reference is None or price is None:
        return 0.0
    return max(0.0, (float(reference) - price) * float(item.qty_per_run))


def is_reportable(verdict: Verdict, obs: PriceObservation) -> bool:
    """Ce qui a le droit d'apparaître comme offre dans le compte rendu.

    Le verdict n'est pas un rejet, l'observation est vérifiée en drive
    (invariant central du §3), ET aucun doute ne subsiste : une contrainte
    dure invérifiable (C-HARD) ou un prix incohérent (C-CHECK) envoient en
    « à vérifier », jamais en liste de courses. Vécu : une litière au type
    inconnu s'est retrouvée « record » dans le panier de Charlotte.
    """
    if verdict.status is Status.REJECT or not obs.is_actionable:
        return False
    return not any(rule in ("C-HARD", "C-CHECK") for rule in verdict.rules)

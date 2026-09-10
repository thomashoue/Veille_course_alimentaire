"""Affectation offre → magasin → personne — §2.1 et règle P8.

Ce n'est pas une veille promo : c'est un optimiseur d'affectation panier →
magasin sous contrainte de géographie. Un magasin n'a de valeur que s'il est
sur un corridor, et un détour ne se paie que s'il rapporte plus que le
carburant.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .models import Grade, Offer, Store
from .units import format_eur
from .validate import p8_worth_detour

PERSON_LABEL = {
    "thomas": "Thomas (corridor ouest — Ploufragan)",
    "charlotte": "Charlotte (corridor est — Rennes)",
    "household": "Domicile (Montauban)",
}


class ExcludedBannerLeak(AssertionError):
    """Une enseigne exclue est arrivée en sortie : bug, jamais une donnée."""


@dataclass
class StoreBasket:
    store: Store
    offers: list[Offer] = field(default_factory=list)
    saving_eur: float = 0.0          # économie vs seuil « bon » (affichage)
    marginal_saving_eur: float = 0.0  # ce qu'on perdrait à faire ses courses ailleurs
    detour_cost_eur: float = 0.0
    net_gain_eur: float = 0.0
    kept: bool = True
    drop_reason: str = ""

    @property
    def assignee(self) -> str:
        return self.store.assignee

    @property
    def n_items(self) -> int:
        return len(self.offers)


@dataclass
class BasketCost:
    """Le seul chiffre qui répond à « aurais-je pu payer moins ? ».

    On chiffre le même panier de deux façons, à quantité égale (prix normalisé ×
    quantité du run, ce qui neutralise les différences de format) :
      * ``best_single_total`` — tout dans le meilleur magasin UNIQUE ;
      * ``split_total``       — chaque article à son meilleur prix, magasins
                                confondus (l'éclatement).
    ``real_gain`` = ce que l'éclatement fait gagner sur le meilleur magasin
    unique. C'est un gain réel, pas la somme d'écarts marginaux.
    """
    n_items: int
    split_total: float
    per_store_total: dict[str, float]
    best_single_store: str | None
    best_single_total: float
    best_single_covers_all: bool

    @property
    def real_gain(self) -> float:
        return max(0.0, self.best_single_total - self.split_total)

    @property
    def real_gain_pct(self) -> float:
        return 100.0 * self.real_gain / self.best_single_total if self.best_single_total else 0.0


def basket_costs(offers: list[Offer], config: Config) -> BasketCost | None:
    """Chiffre le panier : meilleur magasin unique vs éclaté au meilleur prix.

    À quantité égale (``qty_per_run`` × prix normalisé), donc un format de 1 kg
    et une boîte de 250 g se comparent honnêtement. Un magasin qui ne vend pas
    un article se voit prêter le meilleur prix trouvé ailleurs pour cet article
    (neutre) ; on signale alors qu'il ne couvre pas tout le panier.
    """
    by_item: dict[str, dict[str, float]] = {}
    for o in offers:
        if o.unit_price is None:
            continue
        prices = by_item.setdefault(o.item.id, {})
        if o.store_id not in prices or o.unit_price < prices[o.store_id]:
            prices[o.store_id] = o.unit_price
    if not by_item:
        return None

    qty = {iid: float(config.items[iid].qty_per_run) for iid in by_item}
    cheapest = {iid: min(prices.values()) for iid, prices in by_item.items()}
    split_total = sum(cheapest[iid] * qty[iid] for iid in by_item)

    stores = {sid for prices in by_item.values() for sid in prices}
    per_store: dict[str, float] = {}
    covers_all: dict[str, bool] = {}
    for sid in stores:
        total = 0.0
        has_all = True
        for iid, prices in by_item.items():
            if sid in prices:
                total += prices[sid] * qty[iid]
            else:
                total += cheapest[iid] * qty[iid]   # prêté au meilleur prix
                has_all = False
        per_store[sid] = round(total, 2)
        covers_all[sid] = has_all

    # Meilleur magasin unique : d'abord ceux qui couvrent tout le panier ; à
    # défaut, le moins cher en prêtant les articles manquants.
    full = {s: t for s, t in per_store.items() if covers_all[s]}
    pool = full or per_store
    best = min(pool, key=pool.get)
    return BasketCost(
        n_items=len(by_item),
        split_total=round(split_total, 2),
        per_store_total=per_store,
        best_single_store=best,
        best_single_total=per_store[best],
        best_single_covers_all=covers_all[best],
    )


@dataclass
class Plan:
    baskets: list[StoreBasket]
    unmatched: list[str] = field(default_factory=list)      # aucun prix conforme
    out_of_scope: list[str] = field(default_factory=list)   # F&L, hors drive
    dropped: list[StoreBasket] = field(default_factory=list)
    # Offre conforme trouvée, mais uniquement dans un magasin écarté pour
    # cause de détour : ce n'est PAS une absence d'offre, et le dire
    # autrement ferait passer un arbitrage de trajet pour un échec de veille.
    deferred: dict[str, Offer] = field(default_factory=dict)
    cost: BasketCost | None = None

    def by_assignee(self) -> dict[str, list[StoreBasket]]:
        grouped: dict[str, list[StoreBasket]] = {}
        for basket in self.baskets:
            grouped.setdefault(basket.assignee, []).append(basket)
        for baskets in grouped.values():
            baskets.sort(key=lambda b: (b.store.detour_km, b.store.name))
        return grouped

    @property
    def total_saving(self) -> float:
        return sum(b.saving_eur for b in self.baskets)

    @property
    def total_net_gain(self) -> float:
        return sum(b.net_gain_eur for b in self.baskets)


# --------------------------------------------------------------------------- #
def actionable_offers(offers: list[Offer]) -> list[Offer]:
    """Seules les offres vérifiées en drive et pas au-dessus du plafond passent."""
    return [
        o
        for o in offers
        if o.observation.is_actionable
        and not o.verdict.rejected
        and o.grade is not Grade.TOO_HIGH
        and o.unit_price is not None
    ]


def best_per_item(offers: list[Offer], open_store_ids: set[str]) -> dict[str, Offer]:
    """Meilleure offre par article, parmi les magasins encore ouverts.

    À prix égal, on préfère le magasin qui coûte le moins de détour — c'est
    l'arbitrage qui était fait à la main.
    """
    best: dict[str, Offer] = {}
    for offer in offers:
        if offer.store_id not in open_store_ids:
            continue
        current = best.get(offer.item.id)
        if current is None:
            best[offer.item.id] = offer
            continue
        key_new = (offer.unit_price, offer.observation.store_id)
        key_cur = (current.unit_price, current.observation.store_id)
        if key_new < key_cur:
            best[offer.item.id] = offer
    return best


def _alternatives(candidates: list[Offer]) -> dict[str, list[tuple[float, str]]]:
    """Par article, les (prix normalisé, magasin) triés, tous magasins confondus."""
    by_item: dict[str, list[tuple[float, str]]] = {}
    for offer in candidates:
        if offer.unit_price is None:
            continue
        by_item.setdefault(offer.item.id, []).append((offer.unit_price, offer.store_id))
    for prices in by_item.values():
        prices.sort()
    return by_item


def _marginal_saving(offer: Offer, alternatives: dict[str, list[tuple[float, str]]]) -> float:
    """Ce qu'on paierait en plus pour cet article au meilleur AUTRE magasin.

    Article unique à ce magasin → grosse valeur (le perdre coûte de le racheter
    ailleurs, ou de s'en passer) : on retient l'écart au seuil comme plancher.
    """
    prices = alternatives.get(offer.item.id, [])
    here = offer.unit_price
    if here is None:
        return offer.saving_eur
    ailleurs = [p for p, sid in prices if sid != offer.store_id]
    if not ailleurs:
        # introuvable ailleurs : ce n'est pas un arbitrage de prix
        return max(offer.saving_eur, here * float(offer.item.qty_per_run) * 0.0)
    return max(0.0, (min(ailleurs) - here) * float(offer.item.qty_per_run))


def assign(offers: list[Offer], config: Config) -> Plan:
    """Construit la liste par magasin.

    Boucle de fermeture : on affecte chaque article à son meilleur prix, on
    calcule le gain net de chaque magasin (P8), on ferme le magasin le moins
    rentable, et on recommence — les articles se replient alors sur le
    deuxième meilleur prix. On s'arrête quand tout ce qui reste tient debout.
    """
    candidates = [o for o in actionable_offers(offers) if not config.is_excluded(o.store_id)]
    open_stores = {o.store_id for o in candidates}
    dropped: list[StoreBasket] = []
    chosen: dict[str, Offer] = {}
    baskets: dict[str, StoreBasket] = {}

    while True:
        chosen = best_per_item(candidates, open_stores)
        alternatives = _alternatives([o for o in candidates if o.store_id in open_stores])
        baskets = {}
        for offer in chosen.values():
            store = config.store(offer.store_id)
            basket = baskets.setdefault(store.id, StoreBasket(store=store))
            basket.offers.append(offer)
            basket.saving_eur += offer.saving_eur
            basket.marginal_saving_eur += _marginal_saving(offer, alternatives)

        worst: StoreBasket | None = None
        for basket in baskets.values():
            # Le détour se juge sur ce qu'on perdrait à aller ailleurs, pas sur
            # l'écart au seuil : un magasin peut n'avoir aucune « affaire » et
            # valoir le détour parce qu'il est simplement moins cher partout.
            worth, net = p8_worth_detour(
                basket.marginal_saving_eur, basket.store.detour_km, config
            )
            basket.detour_cost_eur = basket.marginal_saving_eur - net
            basket.net_gain_eur = net
            # Un magasin sans détour (domicile ou strictement sur le trajet)
            # ne se discute pas : il n'y a rien à amortir.
            if basket.store.detour_km <= 0 or worth:
                continue
            if worst is None or net < worst.net_gain_eur:
                worst = basket

        if worst is None:
            break

        worst.kept = False
        minimum = float(config.param("min_net_gain_eur", 0.0))
        worst.drop_reason = (
            f"détour {worst.store.detour_km:g} km non amorti : "
            f"{format_eur(worst.marginal_saving_eur)} de moins cher qu'ailleurs, moins "
            f"{format_eur(worst.detour_cost_eur)} de carburant = "
            f"{format_eur(worst.net_gain_eur)} de gain net, sous le minimum de "
            f"{format_eur(minimum)}"
        )
        open_stores.discard(worst.store.id)
        dropped.append(worst)
        if not open_stores:
            chosen, baskets = {}, {}
            break

    plan_baskets = sorted(
        baskets.values(), key=lambda b: (b.store.assignee, b.store.detour_km, b.store.name)
    )
    for basket in plan_baskets:
        basket.offers.sort(key=lambda o: (o.item.category, o.item.label))

    covered = set(chosen)
    # Meilleure offre connue par article, magasins écartés compris.
    fallback: dict[str, Offer] = {}
    for offer in candidates:
        current = fallback.get(offer.item.id)
        if current is None or (offer.unit_price or 0) < (current.unit_price or 0):
            fallback[offer.item.id] = offer

    unmatched: list[str] = []
    deferred: dict[str, Offer] = {}
    for item in config.items.values():
        if item.out_of_scope_drive or item.id in covered:
            continue
        if item.id in fallback:
            deferred[item.id] = fallback[item.id]
        else:
            unmatched.append(item.id)
    out_of_scope = [item.id for item in config.items.values() if item.out_of_scope_drive]

    plan = Plan(
        baskets=plan_baskets,
        unmatched=unmatched,
        out_of_scope=out_of_scope,
        dropped=dropped,
        deferred=deferred,
        cost=basket_costs(candidates, config),
    )
    assert_no_excluded(plan, config)
    return plan


def assert_no_excluded(plan: Plan, config: Config) -> None:
    """Assertion de sortie (§2.1) : aucune enseigne exclue, jamais, nulle part."""
    for basket in plan.baskets:
        if config.is_excluded(basket.store.id) or basket.store.banner.lower() in config.excluded_banners:
            raise ExcludedBannerLeak(
                f"enseigne exclue en sortie : {basket.store.name} ({basket.store.banner})"
            )
        for offer in basket.offers:
            banner = (offer.observation.banner or "").lower()
            if banner in config.excluded_banners:
                raise ExcludedBannerLeak(
                    f"offre d'enseigne exclue en sortie : {offer.observation.product_label} ({banner})"
                )


def person_label(assignee: str) -> str:
    return PERSON_LABEL.get(assignee, assignee)

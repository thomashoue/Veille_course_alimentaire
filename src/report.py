"""Compte rendu — §8. Une liste PAR MAGASIN, pas par catégorie de produit.

Trois sorties depuis le même plan :
  * un markdown complet (le document de référence du vendredi soir) ;
  * un bloc copier-coller pour WhatsApp, en tête ;
  * un fichier .eml prêt à envoyer (aucun identifiant n'est stocké : l'envoi
    reste un geste humain, comme le paiement du drive).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

from .assign import Plan, StoreBasket, person_label
from .config import Config
from .models import Grade, Offer
from .units import format_eur, format_price

GRADE_MARK = {
    Grade.STOCK: "🟢 STOCKER",
    Grade.GOOD: "✅ bon",
    Grade.NORMAL: "▫️ prix normal",
    Grade.TOO_HIGH: "⛔ trop cher",
}


@dataclass
class Report:
    markdown: str
    whatsapp: str
    subject: str
    generated_at: datetime

    def write(self, directory: Path | str) -> dict[str, Path]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = self.generated_at.strftime("%Y-%m-%d")
        paths = {
            "markdown": directory / f"veille-{stamp}.md",
            "whatsapp": directory / f"veille-{stamp}-whatsapp.txt",
        }
        paths["markdown"].write_text(self.markdown, encoding="utf-8")
        paths["whatsapp"].write_text(self.whatsapp, encoding="utf-8")
        return paths

    def to_eml(self, recipients: list[str], sender: str = "veille-courses@localhost") -> str:
        message = EmailMessage()
        message["Subject"] = self.subject
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        message["Date"] = self.generated_at.strftime("%a, %d %b %Y %H:%M:%S %z") or ""
        message.set_content(self.markdown)
        return message.as_string()


# --------------------------------------------------------------------------- #
def _offer_line(offer: Offer) -> str:
    obs = offer.observation
    price = offer.unit_price
    bits = [f"**{obs.product_label}** — {format_eur(obs.price_eur)}"]
    if price is not None and obs.unit_price_unit:
        bits.append(f"soit {format_price(price, obs.unit_price_unit)}")
    else:
        bits.append("prix au format non calculable")
    bits.append(GRADE_MARK.get(offer.grade, ""))
    if offer.is_record:
        previous = (
            f" (précédent : {format_price(offer.previous_best, obs.unit_price_unit or '')})"
            if offer.previous_best
            else ""
        )
        bits.append(f"🏆 record{previous}")
    if obs.mechanic:
        bits.append(f"mécanique {obs.mechanic} — prix moyen sur {obs.required_qty}")
    if obs.weight_basis:
        bits.append(f"poids {obs.weight_basis}")
    if offer.verdict.reasons:
        bits.append("⚠ " + offer.verdict.explain())
    return "- " + " · ".join(b for b in bits if b)


def _basket_section(basket: StoreBasket) -> list[str]:
    lines = [
        f"### {basket.store.name} — {basket.store.city}",
        "",
        f"*{basket.n_items} article(s) · économie estimée "
        f"{format_eur(basket.saving_eur)} · détour {basket.store.detour_km:g} km "
        f"({format_eur(basket.detour_cost_eur)}) · gain net "
        f"{format_eur(basket.net_gain_eur)}*",
        "",
    ]
    if basket.store.has_drive and basket.store.drive_base_url:
        lines += [f"Drive : {basket.store.drive_base_url}", ""]
    else:
        lines += ["*Pas de drive : liste papier, achat en magasin.*", ""]
    lines += [_offer_line(offer) for offer in basket.offers]
    lines.append("")
    return lines


def build_report(
    plan: Plan,
    config: Config,
    *,
    pistes: list[Offer] | None = None,
    observed_item_ids: set[str] | None = None,
    generated_at: datetime | None = None,
    pickup_date: date | None = None,
) -> Report:
    """Assemble le compte rendu à partir du plan d'affectation."""
    now = generated_at or datetime.now()
    lines: list[str] = [
        f"# Veille courses — {now:%A %d %B %Y %Hh%M}",
        "",
        f"Économie estimée : **{format_eur(plan.total_saving)}** · "
        f"gain net après carburant : **{format_eur(plan.total_net_gain)}**",
        "",
    ]
    if pickup_date:
        lines += [f"Date de retrait retenue pour les avantages carte : **{pickup_date:%d/%m/%Y}**", ""]

    whatsapp = _whatsapp_block(plan, config, now)
    lines += ["## Bloc WhatsApp (copier-coller)", "", "```", whatsapp, "```", ""]

    lines += ["## Listes par magasin", ""]
    grouped = plan.by_assignee()
    for assignee in ("household", "charlotte", "thomas"):
        baskets = grouped.get(assignee)
        if not baskets:
            continue
        lines += [f"## {person_label(assignee)}", ""]
        for basket in baskets:
            lines += _basket_section(basket)

    # §10 : savoir conclure à l'absence d'offre plutôt que proposer un pis-aller.
    if plan.unmatched:
        detailed, quiet = _split_unmatched(plan.unmatched, config, observed_item_ids or set())
        lines += ["## Aucune offre conforme cette semaine", ""]
        for item_id in detailed:
            item = config.items[item_id]
            advice = f" — {item.fallback_advice}" if item.fallback_advice else ""
            constraint = (
                f" *(contrainte : {', '.join(item.hard_constraints)})*"
                if item.hard_constraints
                else ""
            )
            lines.append(f"- **{item.label}** : rien de conforme{constraint}{advice}")
        if quiet:
            # Le reste du panier tient en une ligne : rien à en dire cette
            # semaine, et une liste de 25 « rien de conforme » ne se lit pas.
            lines += [
                "",
                "Rien de neuf sur : "
                + ", ".join(config.items[i].label for i in quiet)
                + ".",
            ]
        lines.append("")

    if plan.dropped:
        lines += ["## Magasins écartés (détour non amorti)", ""]
        for basket in plan.dropped:
            lines.append(f"- {basket.store.name} : {basket.drop_reason}")
        lines.append("")

    if plan.deferred:
        lines += [
            "## Conforme, mais pas cette semaine",
            "",
            "*Une offre conforme existe, uniquement dans un magasin qui ne vaut "
            "pas le détour. À prendre si vous y passez pour autre chose.*",
            "",
        ]
        for item_id, offer in plan.deferred.items():
            store = config.store(offer.store_id)
            price = (
                format_price(offer.unit_price, offer.observation.unit_price_unit)
                if offer.unit_price is not None
                else "prix non calculable"
            )
            lines.append(
                f"- **{config.items[item_id].label}** — {offer.observation.product_label} "
                f"à {format_eur(offer.observation.price_eur)} ({price}) chez {store.name}"
            )
        lines.append("")

    if plan.out_of_scope:
        stores = ", ".join(
            config.store(s).name for s in config.out_of_scope_stores if s in config.stores
        )
        lines += [
            "## Hors périmètre drive (fruits & légumes)",
            "",
            f"À prendre en direct ({stores or 'marché'}), sans veille prix :",
            "",
            ", ".join(config.items[i].label for i in plan.out_of_scope),
            "",
        ]

    if pistes:
        lines += [
            "## À vérifier avant achat",
            "",
            "*Rien ici n'est achetable les yeux fermés : piste de catalogue "
            "jamais confirmée en drive, contrainte invérifiable sur le "
            "libellé, ou prix incohérent à la lecture.*",
            "",
        ]
        for offer in pistes:
            obs = offer.observation
            lines.append(
                f"- {obs.product_label} — {format_eur(obs.price_eur)} "
                f"({obs.store_id}, source {obs.source}) · {offer.verdict.explain()}"
            )
        lines.append("")

    subject = (
        f"Veille courses {now:%d/%m} — {sum(b.n_items for b in plan.baskets)} articles, "
        f"{format_eur(plan.total_saving)} d'économie"
    )
    return Report("\n".join(lines).rstrip() + "\n", whatsapp, subject, now)


def _split_unmatched(
    unmatched: list[str], config: Config, observed: set[str]
) -> tuple[list[str], list[str]]:
    """Sépare ce qui mérite une phrase de ce qui tient dans une énumération.

    Méritent une phrase : les articles sous contrainte dure (la litière, dont
    l'absence d'offre conforme est une conclusion en soi), les postes à
    stocker, et tout ce sur quoi on a effectivement relevé quelque chose.
    """
    detailed, quiet = [], []
    for item_id in unmatched:
        item = config.items[item_id]
        if item.hard_constraints or item.bulk_worthy or item_id in observed:
            detailed.append(item_id)
        else:
            quiet.append(item_id)
    return detailed, quiet


def _whatsapp_block(plan: Plan, config: Config, now: datetime) -> str:
    """Version courte : ce qu'on lit sur un téléphone, en une écran."""
    lines = [f"🛒 Courses {now:%d/%m} — {format_eur(plan.total_saving)} d'économie"]
    grouped = plan.by_assignee()
    for assignee in ("household", "charlotte", "thomas"):
        baskets = grouped.get(assignee)
        if not baskets:
            continue
        who = {"household": "Domicile", "charlotte": "Charlotte", "thomas": "Thomas"}[assignee]
        lines.append("")
        lines.append(f"— {who} —")
        for basket in baskets:
            items = ", ".join(o.item.label for o in basket.offers)
            lines.append(f"{basket.store.name} : {items}")
    if plan.unmatched:
        missing = ", ".join(config.items[i].label for i in plan.unmatched[:6])
        more = "…" if len(plan.unmatched) > 6 else ""
        lines += ["", f"Rien de conforme : {missing}{more}"]
    return "\n".join(lines)

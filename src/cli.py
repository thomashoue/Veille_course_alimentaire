"""Interface en ligne de commande.

    python -m src.cli run --offline --manual data/manual.json
    python -m src.cli check --item litiere_chat --store superu_breteil \
        --label "Litière agglomérante charbon actif 5 L" --price 4.59
    python -m src.cli login --banner leclerc
    python -m src.cli search --store leclerc_pleumeleuc --query "lait demi-écrémé"
    python -m src.cli history --item lait_demi_ecreme
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from .config import DATA_DIR, get_config
from .ledger import Ledger
from .models import PriceObservation
from .normalize import normalize
from .units import format_eur, format_price
from .validate import grade, saving_vs_threshold, validate


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


# --------------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace) -> int:
    from .pipeline import run

    config = get_config(args.config)
    pickup = date.fromisoformat(args.pickup) if args.pickup else None
    result = run(
        config,
        item_ids=args.items,
        manual_file=args.manual,
        pickup_date=pickup,
        use_drive=not args.no_drive,
        offline=args.offline,
        headless=not args.headful,
        report_dir=args.out or (DATA_DIR / "reports"),
    )
    print(result.report.markdown)
    print("\n---", file=sys.stderr)
    print(result.stats.summary(), file=sys.stderr)
    print(json.dumps(result.counts(), ensure_ascii=False), file=sys.stderr)

    if args.eml:
        recipients = (config.sources.get("report", {}) or {}).get("recipients", [])
        Path(args.eml).write_text(result.report.to_eml(recipients), encoding="utf-8")
        print(f"e-mail écrit dans {args.eml} (envoi manuel : aucun identifiant "
              "SMTP n'est stocké)", file=sys.stderr)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Valide un relevé unique — l'outil de vérification à la main."""
    config = get_config(args.config)
    obs = PriceObservation(
        store_id=args.store,
        basket_item_id=args.item,
        product_label=args.label,
        price_eur=args.price,
        regular_price=args.regular,
        mechanic=args.mechanic,
        weight_basis=args.weight_basis,
        loyalty_pct=args.loyalty,
        loyalty_valid_until=date.fromisoformat(args.loyalty_until) if args.loyalty_until else None,
        verified_in_drive=args.verified,
    )
    pickup = date.fromisoformat(args.pickup) if args.pickup else None
    normalize(obs, config, pickup_date=pickup)
    verdict = validate(obs, config, pickup_date=pickup)
    note = grade(obs, config)

    print(f"Article        : {config.item(args.item).label}")
    print(f"Magasin        : {config.store(args.store).name}")
    print(f"Format lu      : {obs.pack_label()}")
    if obs.unit_price is not None:
        print(f"Prix normalisé : {format_price(obs.unit_price, obs.unit_price_unit)}")
    if obs.effective_unit_price is not None and obs.mechanic:
        print(
            f"Prix effectif  : {format_price(obs.effective_unit_price, obs.unit_price_unit)}"
            f"  (moyenne sur {obs.required_qty} — c'est CE prix qui compte)"
        )
    print(f"Attributs      : {obs.attributes or '—'}")
    print(f"Verdict        : {verdict.status.value.upper()} — {verdict.explain()}")
    if verdict.rejected:
        # Un produit écarté n'a pas de note ni d'économie : le chiffrer
        # reviendrait à le présenter comme une affaire malgré tout.
        print("Note           : — (écarté, ne remonte pas au rapport)")
    else:
        print(f"Note           : {note.value}")
        print(f"Économie/run   : {format_eur(saving_vs_threshold(obs, config))}")
        if not obs.is_actionable:
            print("Actionnable    : NON — piste seulement, pas vue dans le drive")
    for note_text in obs.notes:
        print(f"  · {note_text}")
    return 0 if not verdict.rejected else 1


def cmd_login(args: argparse.Namespace) -> int:
    from .drive.session import interactive_login

    interactive_login(args.banner)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from .drive import get_client

    config = get_config(args.config)
    store = config.store(args.store)
    client = get_client(store.banner, store, headless=not args.headful)
    try:
        for product in client.search(args.query):
            price = format_eur(product.price_eur) if product.price_eur else "prix ?"
            pack = product.pack.describe() if product.pack else "format ?"
            flag = "" if product.available else "  [indisponible]"
            print(f"{price:>10}  {pack:>12}  {product.label}{flag}")
    finally:
        client.close()
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    """Capture ce que le drive renvoie vraiment, pour caler les sélecteurs."""
    from .drive.capture import capture_search

    config = get_config(args.config)
    store = config.store(args.store)
    directory = capture_search(
        store,
        args.query,
        args.out or (DATA_DIR / "captures"),
        headless=args.headless,
        include_cart=args.cart,
    )
    diagnostic = json.loads((directory / "diagnostic.json").read_text(encoding="utf-8"))
    print(f"\nCapture écrite dans {directory}\n")
    print(f"  page          : {diagnostic['page_size']} caractères")
    print(f"  réponses JSON : {diagnostic['xhr_json_captured']}")
    print(f"  produits lus par les sélecteurs actuels : "
          f"{diagnostic.get('products_found_by_current_selectors', 0)}")
    for sample in diagnostic.get("sample", []):
        print(f"    · {sample['price']} — {sample['label']}")
    print(f"\n  → {diagnostic['verdict']}\n")
    print("Les fichiers sont masqués (e-mail, téléphone, numéros longs) et ne")
    print("contiennent ni en-têtes ni cookies. Relisez-les avant de les partager.")
    return 0


def cmd_parse_page(args: argparse.Namespace) -> int:
    """Lit une page de drive enregistrée depuis le navigateur habituel.

    Aucun pilotage : c'est la voie qui reste ouverte quand le drive bloque les
    navigateurs automatisés, et la plus durable des deux.
    """
    from .drive.offline import analyze_page, observations_from_page

    config = get_config(args.config)
    store = config.store(args.store)
    html = Path(args.file).read_text(encoding=args.encoding, errors="replace")

    if args.diagnose:
        # Découverte d'un gabarit inconnu : on n'affiche que de la structure.
        analysis = analyze_page(html)
        print(f"Page : {analysis['page_size']} caractères, "
              f"{analysis['prices_in_page']} prix repérés dans le texte brut")
        print(f"JSON embarqué : {analysis['embedded_json_markers'] or 'aucun marqueur connu'}")
        print("\nÉléments porteurs de prix, les plus fréquents :\n")
        print(f"  {'n':>5}  {'balise':<10} {'classe':<34} extrait")
        for candidate in analysis["candidates"]:
            print(f"  {candidate['count']:>5}  {candidate['tag']:<10} "
                  f"{candidate['class']:<34} {candidate['sample'][:70]}")
        if not analysis["candidates"]:
            print("  (aucun) — la page ne contient peut-être aucun résultat de recherche")
        print("\nCopiez ce tableau dans la conversation : il suffit à écrire le sélecteur.")
        return 0

    observations, report = observations_from_page(html, store, config, source_url=args.url)

    print(f"Page      : {args.file} ({report['page_size']} caractères)")
    print(f"Méthode   : {report['method']}")
    print(f"Produits  : {report['products_found']} lus, "
          f"{report['matched_to_basket']} rattachés au panier, "
          f"{report['ignored_not_in_basket']} hors panier")
    if not observations:
        print("\nRien d'exploitable. Deux causes possibles :")
        print("  · la page enregistrée est la version « HTML seul » sans contenu ;")
        print("  · le gabarit est inconnu — envoyez-moi le fichier, il servira de fixture.")
        return 1

    print()
    for obs in observations:
        print(f"  {obs.price_eur:>7.2f} €  {obs.pack_label():>14}  "
              f"{config.item(obs.basket_item_id).label:<22} {obs.product_label[:55]}")

    out = Path(args.out or (DATA_DIR / "manual.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if out.exists() and args.append:
        existing = json.loads(out.read_text(encoding="utf-8"))
    rows = existing + [obs.to_row() for obs in observations]
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(rows)} relevé(s) écrits dans {out}")
    print(f"Ensuite : python -m src.cli run --no-drive --manual {out}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    config = get_config(args.config)
    ledger = Ledger(args.ledger)
    for row in ledger.history(args.item, limit=args.limit):
        print(
            f"{row['observed_at'][:10]}  {row['store_id']:<24} "
            f"{row['effective_unit_price']:.3f} €/{row['unit_price_unit'] or '?'}  "
            f"{'drive' if row['verified_in_drive'] else 'piste'}  {row['product_label'][:60]}"
        )
    trend = ledger.trend(args.item, weeks=int(config.param("trend_weeks", 12)))
    if trend:
        direction = "baissière" if trend["delta_pct"] < -1 else (
            "haussière" if trend["delta_pct"] > 1 else "stable"
        )
        print(
            f"\nTendance {direction} : {trend['delta_pct']:+.1f} % "
            f"(moyenne {trend['average']:.3f}, récente {trend['recent_average']:.3f}, "
            f"{trend['n']} relevés)"
        )
    ledger.close()
    return 0


def cmd_fill(args: argparse.Namespace) -> int:
    from .pipeline import fill_carts, run

    config = get_config(args.config)
    result = run(
        config,
        manual_file=args.manual,
        offline=args.offline,
        use_drive=not args.no_drive,
        headless=not args.headful,
    )
    filled = fill_carts(
        result.plan,
        config,
        dry_run=not args.commit,
        headless=not args.headful,
        store_ids=args.stores,
    )
    for store_id, lines in filled.items():
        print(f"{config.store(store_id).name} : {len(lines)} ligne(s) au panier")
    print(
        "\nLe panier est rempli. Le créneau et le paiement restent à faire à la main.",
    )
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="veille-courses",
        description="Veille prix et remplissage de drive — foyer Montauban-de-Bretagne",
    )
    parser.add_argument("--config", help="répertoire de config (défaut : ./config)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="run hebdomadaire complet")
    run_parser.add_argument("--items", nargs="*", help="limiter à ces articles")
    run_parser.add_argument("--manual", help="fichier JSON de relevés saisis à la main")
    run_parser.add_argument("--pickup", help="date de retrait (YYYY-MM-DD) pour les avantages carte")
    run_parser.add_argument("--no-drive", action="store_true", help="sauter la vérification drive")
    run_parser.add_argument("--offline", action="store_true", help="cache uniquement, aucun réseau")
    run_parser.add_argument("--headful", action="store_true", help="navigateur visible")
    run_parser.add_argument("--out", help="répertoire de sortie des rapports")
    run_parser.add_argument("--eml", help="écrire aussi un .eml prêt à envoyer")
    run_parser.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="valider un relevé unique")
    check.add_argument("--item", required=True)
    check.add_argument("--store", required=True)
    check.add_argument("--label", required=True)
    check.add_argument("--price", type=float, required=True)
    check.add_argument("--regular", type=float, help="prix habituel annoncé")
    check.add_argument("--mechanic", help="second_-30 | second_-50 | lot | 3_pour_2")
    check.add_argument("--weight-basis", dest="weight_basis", choices=["brut", "net_egoutte"])
    check.add_argument("--loyalty", type=float, help="avantage carte en %%")
    check.add_argument("--loyalty-until", dest="loyalty_until")
    check.add_argument("--pickup", help="date de retrait (YYYY-MM-DD)")
    check.add_argument("--verified", action="store_true", help="produit vu dans le drive")
    check.set_defaults(func=cmd_check)

    login = sub.add_parser("login", help="ouvrir un navigateur pour se connecter soi-même")
    login.add_argument("--banner", required=True, choices=["leclerc", "intermarche", "u"])
    login.set_defaults(func=cmd_login)

    search = sub.add_parser("search", help="rechercher un produit dans un drive")
    search.add_argument("--store", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--headful", action="store_true")
    search.set_defaults(func=cmd_search)

    parse_page = sub.add_parser(
        "parse-page",
        help="lire une page de drive enregistrée depuis le navigateur (Ctrl+S)",
    )
    parse_page.add_argument("--store", required=True)
    parse_page.add_argument("--file", required=True, help="fichier .html enregistré")
    parse_page.add_argument("--url", help="URL d'origine, pour la traçabilité")
    parse_page.add_argument("--out", help="fichier de relevés (défaut : data/manual.json)")
    parse_page.add_argument("--encoding", default="utf-8")
    parse_page.add_argument(
        "--diagnose",
        action="store_true",
        help="analyser la structure d'un gabarit inconnu (sortie courte, sans donnée perso)",
    )
    parse_page.add_argument(
        "--append", action="store_true", help="ajouter aux relevés existants"
    )
    parse_page.set_defaults(func=cmd_parse_page)

    capture = sub.add_parser(
        "capture",
        help="enregistrer ce qu'un drive renvoie (HTML + XHR) pour caler les sélecteurs",
    )
    capture.add_argument("--store", required=True)
    capture.add_argument("--query", required=True)
    capture.add_argument("--out", help="répertoire de sortie (défaut : data/captures)")
    capture.add_argument("--cart", action="store_true", help="capturer aussi la page panier")
    capture.add_argument(
        "--headless",
        action="store_true",
        help="sans fenêtre (déconseillé : on ne voit pas si la session a expiré)",
    )
    capture.set_defaults(func=cmd_capture)

    history = sub.add_parser("history", help="historique et tendance d'un article")
    history.add_argument("--item", required=True)
    history.add_argument("--limit", type=int, default=30)
    history.add_argument("--ledger", help="chemin de la base")
    history.set_defaults(func=cmd_history)

    fill = sub.add_parser("fill", help="remplir les paniers drive (créneau et paiement exclus)")
    fill.add_argument("--manual")
    fill.add_argument("--offline", action="store_true")
    fill.add_argument("--no-drive", action="store_true")
    fill.add_argument("--headful", action="store_true")
    fill.add_argument("--stores", nargs="*")
    fill.add_argument(
        "--commit",
        action="store_true",
        help="écrire vraiment dans le panier (sinon dry-run)",
    )
    fill.set_defaults(func=cmd_fill)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

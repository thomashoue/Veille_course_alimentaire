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
        collect_sources=True if args.collect else None,
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


def _parse_directory(args: argparse.Namespace, config, store) -> int:
    """Toutes les pages d'un dossier, en un seul relevé."""
    from .drive.offline import observations_from_page

    directory = Path(args.dir)
    pages = sorted(directory.glob("*.htm*"))
    if not pages:
        print(f"Aucun fichier .html dans {directory}")
        return 1

    seen: dict[str, object] = {}
    for page in pages:
        html = page.read_text(encoding=args.encoding, errors="replace")
        observations, report = observations_from_page(html, store, config)
        alerte = "" if report.get("store_city_seen", True) else "  ⚠ ville absente de la page"
        print(f"{page.name:<40} {report['method']:<14} "
              f"{report['matched_to_basket']} relevé(s){alerte}")
        for obs in observations:
            seen[obs.id] = obs

    if not seen:
        print("\nRien d'exploitable dans ce dossier.")
        return 1

    out = Path(args.out or (DATA_DIR / "manual.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if out.exists() and args.append:
        existing = json.loads(out.read_text(encoding="utf-8"))
    rows = existing + [obs.to_row() for obs in seen.values()]
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(seen)} relevé(s) uniques → {out}")
    print(f"Ensuite : python -m src.cli run --no-drive --manual {out}")
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


def _read_clipboard() -> str:
    """Contenu du presse-papiers, sans dépendance externe."""
    import subprocess

    if sys.platform == "win32":
        command = [
            "powershell", "-noprofile", "-command",
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-Clipboard -Raw",
        ]
    elif sys.platform == "darwin":
        command = ["pbpaste"]
    else:
        command = ["xclip", "-selection", "clipboard", "-o"]
    result = subprocess.run(command, capture_output=True, timeout=10)
    return result.stdout.decode("utf-8", errors="replace")


def _extract_json_array(text: str) -> str:
    """Isole le tableau JSON d'une réponse d'assistant (souvent entouré de prose)."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("aucun tableau JSON [ … ] trouvé dans le texte collé")
    return text[start : end + 1]


def _find_chromium() -> str | None:
    """Localise un navigateur Chromium (Chrome/Edge/Brave) pour --new-window.

    Chrome ignore souvent la demande de nouvelle fenêtre passée par la voie
    générique du module webbrowser ; en l'appelant directement avec
    --new-window, une fenêtre neuve est garantie, séparée des onglets ouverts.
    """
    import os
    import shutil

    for name in ("google-chrome", "chromium", "chromium-browser", "brave-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == "win32":
        candidates = []
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(var, "")
            if base:
                candidates += [
                    os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                    os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                    os.path.join(base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                ]
        for path in candidates:
            if os.path.exists(path):
                return path
    elif sys.platform == "darwin":
        for path in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ):
            if os.path.exists(path):
                return path
    return None


def cmd_open_tabs(args: argparse.Namespace) -> int:
    """Ouvre les recherches de drive comme onglets dans le navigateur PAR DÉFAUT.

    Aucun cookie n'est extrait ni copié : les onglets s'ouvrent dans VOTRE
    navigateur, celui où vous êtes déjà connecté, qui apporte lui-même sa
    session. C'est la seule automatisation qui a un sens ici — les drives
    bloquent l'automatisation, pas votre navigateur.

    Optionnellement, écrit un .bat/.sh rejouable au lieu d'ouvrir tout de suite.
    """
    import webbrowser

    config = get_config(args.config)
    store = config.store(args.store)

    if args.items:
        items = [config.item(i) for i in args.items if i in config.items]
    elif args.bulk:
        items = [i for i in config.items.values() if i.bulk_worthy]
    else:
        items = [
            i for i in config.items.values()
            if not i.out_of_scope_drive and i.category != "fl"
        ]

    urls: list[tuple[str, str]] = []
    for item in items:
        query = item.keywords[0] if item.keywords else item.label
        url = store.search_url(query)
        if url:
            urls.append((item.label, url))

    if not urls:
        print(f"Aucune URL de recherche pour {store.name} (drive non configuré ?).")
        return 1

    if args.script:
        path = Path(args.script)
        if sys.platform == "win32":
            lines = ["@echo off", f"rem Recherches drive — {store.name}",
                     "rem Nouvelle fenêtre du navigateur par défaut, puis onglets"]
            # start "" <url> ouvre dans le navigateur par défaut ; on force une
            # fenêtre neuve via le protocole en ouvrant le premier seul.
            lines += [f'start "" "{url}"' for _, url in urls]
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            lines = ["#!/bin/sh", f"# Recherches drive — {store.name}"]
            lines += [f'{opener} "{url}"' for _, url in urls]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"{len(urls)} recherche(s) écrites dans {path} — lancez-le quand vous voulez.")
        return 0

    same_window = args.same_window
    only_urls = [url for _, url in urls]
    for label, _ in urls:
        print(f"  · {label}")

    chromium = None if same_window else _find_chromium()
    if chromium:
        # Une seule fenêtre neuve avec tous les onglets, séparée de l'existant.
        import subprocess

        print(f"\nNouvelle fenêtre ({store.name}) — {len(only_urls)} onglet(s).")
        subprocess.Popen([chromium, "--new-window", *only_urls])
    elif same_window:
        print(f"\nOuverture dans les onglets courants ({store.name})…")
        for url in only_urls:
            webbrowser.open_new_tab(url)
    else:
        # Pas de Chromium trouvé : au mieux, new=1 pour le premier.
        print(f"\nNouvelle fenêtre ({store.name}) via le navigateur par défaut…")
        for i, url in enumerate(only_urls):
            webbrowser.open(url, new=1 if i == 0 else 2, autoraise=(i == 0))
    print("Aucun cookie n'est copié : c'est votre navigateur, déjà connecté.")
    print("\nEnregistrez les pages (Ctrl+S, ou SingleFile « tous les onglets »), puis :")
    print(f"  python -m src.cli parse-page --store {store.id} --dir <dossier>")
    return 0


def cmd_paste(args: argparse.Namespace) -> int:
    """Importe des relevés JSON depuis le presse-papiers (ou un fichier/stdin).

    C'est le chaînon avec Claude dans Chrome : l'extension ne peut pas
    enregistrer de fichier (pas de Ctrl+S pour une extension), mais elle rend
    du JSON dans la conversation. Copier sa réponse puis `paste` suffit.
    """
    from .models import PriceObservation
    from .pipeline import load_observations

    config = get_config(args.config)
    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8", errors="replace")
    elif args.stdin:
        raw = sys.stdin.read()
    else:
        try:
            raw = _read_clipboard()
        except Exception as exc:
            print(f"Presse-papiers illisible ({exc}) — utilisez --file ou --stdin.")
            return 2

    try:
        rows = json.loads(_extract_json_array(raw))
    except ValueError as exc:
        print(f"Rien d'importable : {exc}")
        return 2

    known_fields = set(PriceObservation.__dataclass_fields__)
    valid, errors = [], []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"entrée {index} : pas un objet")
            continue
        store = row.get("store_id")
        item = row.get("basket_item_id")
        if store not in config.stores:
            errors.append(f"entrée {index} : magasin inconnu {store!r}")
            continue
        if item not in config.items:
            errors.append(f"entrée {index} : article inconnu {item!r}")
            continue
        if not row.get("product_label") or row.get("price_eur") is None:
            errors.append(f"entrée {index} : libellé ou prix manquant")
            continue
        valid.append({k: v for k, v in row.items() if k in known_fields})

    if errors:
        print("Écartées :")
        for error in errors:
            print(f"  · {error}")
        if not valid:
            print(f"\nMagasins valides : {', '.join(sorted(config.stores))}")
            print(f"Articles valides : {', '.join(sorted(config.items))}")
            return 1

    out = Path(args.out or (DATA_DIR / "manual.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if out.exists() and not args.replace:
        existing = json.loads(out.read_text(encoding="utf-8"))
    merged = existing + valid
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    for row in valid:
        print(f"  {row['price_eur']:>7.2f} €  {config.item(row['basket_item_id']).label:<22} "
              f"{row['product_label'][:60]}")
    print(f"\n{len(valid)} relevé(s) importés → {out} ({len(merged)} au total)")
    print(f"Ensuite : python -m src.cli run --no-drive --manual {out}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Liste ce qui mérite une vérification sur la fiche produit — et rien d'autre.

    Le pipeline sait dire quand il doute : prix incohérent, format ou base de
    poids absents, produit jamais confirmé en drive. Cette commande rassemble
    ces cas, avec l'URL quand on l'a, et un prompt prêt pour Claude dans Chrome :
    l'extension ouvre ces fiches (dans votre navigateur, non bloqué), et rend
    du JSON corrigé que « paste » réabsorbe. On ne vérifie que les doutes.
    """
    from .normalize import normalize
    from .pipeline import load_observations
    from .validate import validate

    config = get_config(args.config)
    observations = load_observations(args.manual, config)

    doutes: list[tuple[str, PriceObservation]] = []
    for obs in observations:
        if obs.basket_item_id not in config.items:
            continue
        normalize(obs, config)
        verdict = validate(obs, config)
        raisons = []
        if obs.suspect_reason:
            raisons.append(obs.suspect_reason)
        # Règles « à confirmer » : format absent (P4), base de poids (P5),
        # avantage carte hors fenêtre (P6), promo trompeuse (P7), non vérifié (C1).
        for rule, reason in zip(verdict.rules, verdict.reasons):
            if rule in ("P4", "P5", "P6", "P7", "C1", "C-CHECK"):
                raisons.append(f"[{rule}] {reason}")
        if raisons:
            doutes.append(("; ".join(raisons), obs))

    if not doutes:
        print("Aucun doute : tous les relevés sont exploitables tels quels.")
        return 0

    print(f"{len(doutes)} relevé(s) à vérifier sur la fiche produit :\n")
    urls = []
    for raison, obs in doutes:
        store = config.store(obs.store_id)
        print(f"  · {config.item(obs.basket_item_id).label} — {obs.product_label[:50]}")
        print(f"    {store.name} · {raison}")
        if obs.source_url:
            print(f"    {obs.source_url}")
            urls.append(obs.source_url)
        print()

    if args.prompt:
        print("=" * 68)
        print("PROMPT pour Claude dans Chrome (copier-coller) :")
        print("=" * 68)
        print(
            "Ouvre chacune de ces fiches produit (je suis connecté) et relève "
            "SANS CALCULER : libellé exact, prix de la boîte affiché, grammage "
            "(en précisant « net égoutté » si mentionné), mécanique promo "
            "éventuelle. Rends un tableau JSON, un objet par produit, avec les "
            "champs store_id, basket_item_id, product_label, price_eur, "
            "pack_size, pack_unit, weight_basis, verified_in_drive:true, "
            'source:"drive". URLs :'
        )
        for url in urls or ["(aucune URL relevée — ouvre les fiches à la main)"]:
            print(f"  {url}")
    else:
        print("Ajoutez --prompt pour un texte prêt à coller dans Claude dans Chrome.")
    print("\nPuis : python -m src.cli paste   (recolle le JSON corrigé)")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Analyse un panier multi-enseignes : face-à-face par article + affectation.

    Nourri par des relevés (--manual), c'est le « test 3 enseignes » sur VOS
    prix : chaque article comparé enseigne par enseigne sur le prix normalisé,
    puis l'affectation par corridor avec l'arbitrage du détour.
    """
    from collections import defaultdict

    from .assign import assign, person_label
    from .pipeline import build_offers, load_observations, shortlist
    from .units import format_eur, format_price

    config = get_config(args.config)
    observations = load_observations(args.manual, config)
    kept, _ = shortlist(observations, config)
    offers, pistes = build_offers(kept, config)
    plan = assign(offers, config)

    print("=" * 68)
    print("FACE-À-FACE PAR ARTICLE — meilleur prix normalisé retenu")
    print("=" * 68)
    par_article = defaultdict(list)
    for offer in offers:
        par_article[offer.item.id].append(offer)
    for item_id, offs in par_article.items():
        item = config.item(item_id)
        seuil = config.threshold(item_id).get("good", "—")
        print(f"\n{item.label}  (seuil bon : {seuil} €/{item.base_unit})")
        offs.sort(key=lambda o: o.unit_price if o.unit_price is not None else 9e9)
        for i, offer in enumerate(offs):
            store = config.store(offer.store_id)
            up = (format_price(offer.unit_price, offer.observation.unit_price_unit)
                  if offer.unit_price is not None else "n/c")
            flags = []
            if offer.observation.weight_basis == "brut":
                flags.append("brut→égoutté")
            if offer.observation.loyalty_pct:
                flags.append(f"carte {offer.observation.loyalty_pct:g}%")
            flag = f"  [{', '.join(flags)}]" if flags else ""
            mark = "   ← retenu" if i == 0 else ""
            print(f"   {offer.observation.price_eur:>6.2f} €  {up:>12}  "
                  f"{store.name[:24]:<24} {offer.grade.value}{flag}{mark}")
        if len(offs) > 1:
            best, worst = offs[0], offs[-1]
            if best.unit_price and worst.unit_price:
                ecart = (worst.unit_price - best.unit_price) / worst.unit_price
                print(f"   → écart {ecart:.0%} entre le meilleur et le pire")

    print("\n" + "=" * 68)
    print("AFFECTATION — par personne, corridor et détour")
    print("=" * 68)
    grouped = plan.by_assignee()
    for who in ("household", "charlotte", "thomas"):
        baskets = grouped.get(who)
        if not baskets:
            continue
        print(f"\n### {person_label(who)}")
        for basket in baskets:
            mini = (f" · min commande {format_eur(basket.store.min_order_eur)}"
                    if basket.store.min_order_eur else "")
            total = sum(o.observation.price_eur for o in basket.offers)
            alerte = ""
            if basket.store.min_order_eur and total < basket.store.min_order_eur:
                alerte = f"  ⚠ panier {format_eur(total)} SOUS le minimum"
            print(f"  {basket.store.name} — {basket.n_items} art. · "
                  f"gain net {format_eur(basket.net_gain_eur)} · "
                  f"détour {basket.store.detour_km:g} km{mini}{alerte}")
            for offer in basket.offers:
                print(f"      - {offer.item.label}: {offer.observation.product_label} "
                      f"({format_eur(offer.observation.price_eur)})")
    if plan.dropped:
        print("\n  Magasins écartés :")
        for basket in plan.dropped:
            print(f"    - {basket.store.name} : {basket.drop_reason}")
    if plan.deferred:
        print("\n  Conforme mais pas cette semaine (détour non amorti) :")
        for item_id, offer in plan.deferred.items():
            print(f"    - {config.item(item_id).label} chez "
                  f"{config.store(offer.store_id).name}")

    print(f"\nÉconomie totale estimée : {format_eur(plan.total_saving)} · "
          f"gain net : {format_eur(plan.total_net_gain)}")
    if pistes:
        print(f"{len(pistes)} relevé(s) en « à vérifier » (non actionnables).")
    return 0


def cmd_shortlist(args: argparse.Namespace) -> int:
    """Interroge les agrégateurs et dit QUOI vérifier, et OÙ.

    C'est le constat C1 rendu pratique : on ne vérifie pas tout le panier,
    seulement les pistes que les catalogues font remonter — plus les postes à
    stocker dont le seuil mérite un coup d'œil.
    """
    from .normalize import normalize
    from .pipeline import collect, shortlist
    from .validate import grade as grade_fn

    config = get_config(args.config)
    observations = collect(config, args.items, offline=args.offline)
    kept, _ = shortlist(observations, config)

    par_magasin: dict[str, dict[str, list]] = {}
    for obs in kept:
        store = config.stores.get(obs.store_id)
        if store is None or not store.has_drive:
            continue
        par_magasin.setdefault(store.id, {}).setdefault(obs.basket_item_id, []).append(obs)

    if not par_magasin:
        print("Aucune piste en drive cette semaine (collecte vide ou tout écarté).")
        print("Les postes à stocker restent consultables : run --no-drive --manual …")
        return 0

    total = 0
    for store_id, items in par_magasin.items():
        store = config.store(store_id)
        print(f"\n{store.name} — {len(items)} article(s) à vérifier")
        for item_id, pistes in items.items():
            item = config.item(item_id)
            meilleure = min(p.price_eur for p in pistes)
            query = item.keywords[0] if item.keywords else item.label
            url = store.search_url(query)
            print(f"  · {item.label:<24} annoncé dès {meilleure:.2f} € "
                  f"({len(pistes)} piste(s))")
            if url:
                print(f"    {url}")
            total += 1
    print(f"\n{total} recherche(s) à ouvrir. Pour chaque page : Ctrl+S dans un "
          "dossier, puis :")
    print("  python -m src.cli parse-page --store <magasin> --dir <dossier>")
    return 0


def cmd_parse_page(args: argparse.Namespace) -> int:
    """Lit une page de drive enregistrée depuis le navigateur habituel.

    Aucun pilotage : c'est la voie qui reste ouverte quand le drive bloque les
    navigateurs automatisés, et la plus durable des deux.
    """
    from .drive.offline import analyze_page, observations_from_page

    config = get_config(args.config)
    store = config.store(args.store)

    if args.dir:
        return _parse_directory(args, config, store)
    if not args.file:
        print("Il faut --file (une page) ou --dir (un dossier de pages).")
        return 2

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

    if not report.get("store_city_seen", True):
        print(f"⚠ « {store.city} » n'apparaît nulle part dans la page. Le magasin "
              "actif de la session était peut-être un autre (chez Intermarché, se "
              "connecter ailleurs bascule tout le compte). Vérifiez avant de vous "
              "servir de ces prix.")
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
    print("(ajoutez --collect pour interroger en plus les agrégateurs)")
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
    run_parser.add_argument(
        "--collect",
        action="store_true",
        help="interroger aussi les agrégateurs quand --manual est fourni (long)",
    )
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
    parse_page.add_argument("--file", help="fichier .html enregistré")
    parse_page.add_argument("--dir", help="dossier de pages .html à lire d'un coup")
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

    open_tabs = sub.add_parser(
        "open-tabs",
        help="ouvrir les recherches de drive comme onglets dans le navigateur par défaut",
    )
    open_tabs.add_argument("--store", required=True)
    open_tabs.add_argument("--items", nargs="*", help="articles précis (défaut : tout le drive)")
    open_tabs.add_argument("--bulk", action="store_true", help="seulement les postes à stocker")
    open_tabs.add_argument("--script", help="écrire un .bat/.sh rejouable au lieu d'ouvrir")
    open_tabs.add_argument(
        "--same-window",
        action="store_true",
        help="ajouter aux onglets courants au lieu d'ouvrir une nouvelle fenêtre",
    )
    open_tabs.set_defaults(func=cmd_open_tabs)

    review = sub.add_parser(
        "review",
        help="lister les relevés douteux à vérifier sur la fiche produit (+ prompt extension)",
    )
    review.add_argument("--manual", required=True, help="fichier de relevés (data/manual.json)")
    review.add_argument("--prompt", action="store_true",
                        help="afficher un prompt prêt pour Claude dans Chrome")
    review.set_defaults(func=cmd_review)

    compare = sub.add_parser(
        "compare",
        help="test multi-enseignes : face-à-face par article + affectation, sur vos relevés",
    )
    compare.add_argument("--manual", required=True, help="fichier de relevés (data/manual.json)")
    compare.set_defaults(func=cmd_compare)

    paste = sub.add_parser(
        "paste",
        help="importer des relevés JSON depuis le presse-papiers (réponse de Claude dans Chrome)",
    )
    paste.add_argument("--file", help="lire depuis un fichier plutôt que le presse-papiers")
    paste.add_argument("--stdin", action="store_true", help="lire depuis l'entrée standard")
    paste.add_argument("--out", help="fichier de relevés (défaut : data/manual.json)")
    paste.add_argument("--replace", action="store_true",
                       help="remplacer le fichier au lieu d'ajouter")
    paste.set_defaults(func=cmd_paste)

    shortlist_parser = sub.add_parser(
        "shortlist",
        help="interroger les agrégateurs et lister quoi vérifier en drive, avec les URL",
    )
    shortlist_parser.add_argument("--items", nargs="*", help="limiter à ces articles")
    shortlist_parser.add_argument("--offline", action="store_true")
    shortlist_parser.set_defaults(func=cmd_shortlist)

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

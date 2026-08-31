# Veille courses & remplissage de drive — Synthèse fonctionnelle

**Destination : document d'entrée pour un projet développé sous Claude Code.**
Rédigé le 2026-08-31, à partir de trois semaines d'expérimentation manuelle (29–31 août 2026).
Foyer : Thomas & Charlotte, Montauban-de-Bretagne (35360).

---

## 0. Ce qu'il faut retenir avant d'écrire une ligne de code

Trois constats sont sortis de l'expérimentation. Ils invalident l'architecture « naïve » (scraper des catalogues de promos et envoyer un résumé) et doivent piloter la conception.

| # | Constat vérifié | Conséquence pour le code |
|---|---|---|
| **C1** | **Le catalogue n'est pas l'assortiment du drive.** Vérifié dans 3 drives : sur les offres annoncées par les agrégateurs de prospectus, la majorité n'existe pas dans le drive correspondant (Prince 1,2 kg à 2,51 €, Friskies 2 kg à 4,22 €, Ultima 9 kg à 26,95 €, Bigard 640 g à 9,99 € — tous absents). Les prix de prospectus sont des prix **magasin**. | La source de vérité est **le drive**, pas le prospectus. Le catalogue ne sert qu'à *orienter* la vérification. Toute offre doit porter un flag `verified_in_drive: bool` et rien de non vérifié ne doit remonter comme actionnable. |
| **C2** | **Les agrégateurs publient des prix faux de façon systématique et prévisible.** Notamment : le prix du 2ᵉ article d'une mécanique « −30 % / −50 % / −60 % sur le 2ᵉ » présenté comme le prix promo. | Il faut une **couche de validation** en amont du stockage, avec des règles déterministes (§5). Ce sont les meilleurs candidats à des tests unitaires. |
| **C3** | **Le prix affiché n'est presque jamais comparable d'une enseigne à l'autre** : grammages différents, poids brut vs net égoutté, avantages carte conditionnés à la date de **retrait**, lots. | Le modèle de données doit stocker un **prix normalisé** (€/kg, €/L, €/dose, €/rouleau, €/unité) calculé, pas saisi, plus la base de référence (`brut` / `net_egoutte`). Toute comparaison se fait sur le normalisé. |

---

## 1. Le besoin, en une phrase

Produire chaque vendredi 18h **une liste d'achat par magasin, réellement disponible en drive, répartie entre deux trajets domicile-travail opposés**, puis pouvoir remplir les paniers drive à la demande.

Ce n'est pas une veille promo. C'est un **optimiseur d'affectation panier → magasin sous contrainte de géographie**, alimenté par une veille prix.

---

## 2. Domaine métier

### 2.1 Géographie — la contrainte structurante

Le foyer est sur la N12 entre Rennes et Saint-Brieuc. Les deux actifs font des trajets **opposés** :

```
        OUEST  ◄──────────── N12 ────────────►  EST
   Ploufragan (Thomas, 69 km)   Montauban   Rennes (Charlotte, 32 km)
     Yffiniac 62 · Lamballe 50    (0 km)      Pleumeleuc 11 · Breteil 14
     Broons 29 · Caulnes 20                   L'Hermitage 23 · Pacé 24
```

Un magasin n'a de valeur que s'il est **sur un corridor**. Un détour de 25 km annule une économie de 2,50 € (arbitrage déjà tranché sur Jardiland/Catsan).

**Règle d'affectation (à coder, pas à improviser) :**

| Enseigne | Affecté à | Point |
|---|---|---|
| Intermarché | **Domicile** (défaut) | Montauban, 0 km |
| Intermarché format Hyper | Thomas | Lamballe (50 km) |
| E.Leclerc | Charlotte | Pleumeleuc (11 km) — Thomas en secours à Ploufragan |
| Super U / Hyper U | Charlotte (Breteil, L'Hermitage) ou Thomas (Yffiniac, 62 km) | |
| Aldi, Lidl, Netto, Grand Frais | **Thomas** | tout le discount est groupé autour de Ploufragan/Trégueux/Langueux |
| Action | au choix | Pacé (Charlotte) ou Trégueux (Thomas) |
| Maxi Zoo | Thomas | Langueux, click & collect 2h |

**Exclusions dures : Carrefour et Auchan** (toutes enseignes). Aucune offre de ces enseignes ne doit jamais apparaître en sortie. À implémenter comme un filtre en entrée *et* une assertion en sortie.

### 2.2 Le panier

31 articles relevés dans leur liste réelle (2026-08-30) + un panier type élargi. Caractéristiques :

- **Environ la moitié est non alimentaire** : animalerie (litière, croquettes, sacs), droguerie (lessive, liquide vaisselle, PQ, papier cuisson, vinaigre), hygiène. La veille doit couvrir tout le panier.
- **Postes à stocker** (achat en gros rentable) : lait, légumineuses sèches, conserves de poisson, café, litière, croquettes, lessive, papier toilette.
- **8 fruits/légumes** qui gagnent à être achetés hors drive (Grand Frais, marché).

**Contrainte produit non négociable — litière :** uniquement **silice** ou **agglomérante au charbon actif**. Jamais de minérale simple ni de végétale, même nettement moins chère au litre. C'est un filtre de conformité, pas une préférence : une litière hors de ces deux types n'est pas une affaire, c'est un hors-sujet. Le code doit la traiter comme une **contrainte dure qui précède l'optimisation prix**.

---

## 3. Modèle de données proposé

```python
# --- Référentiel ---
Store(
    id, banner,               # 'leclerc' | 'intermarche' | 'u' | 'lidl' | 'aldi' | 'netto' | 'action' | 'grandfrais' | 'maxizoo'
    name, city, postcode,
    corridor,                 # 'home' | 'east' | 'west'
    assignee,                 # 'household' | 'charlotte' | 'thomas'
    distance_km,
    has_drive: bool,
    drive_base_url,           # ex. fd7-courses.leclercdrive.fr/magasin-173501-173501-Pleumeleuc
    search_url_template,      # ex. '{base}/recherche.aspx?TexteRecherche={q}'
    format,                   # 'super' | 'hyper' | 'express' | 'drive_pur' | 'discount'
    excluded: bool,           # Carrefour / Auchan → True
)

# --- Catalogue produit ---
BasketItem(
    id, label,                # 'litière chat', 'lait demi-écrémé', ...
    category,                 # 'cremerie' | 'animalerie' | 'droguerie' | 'viande' | 'fl' | 'epicerie' | 'biere' | 'hygiene'
    unit,                     # 'kg' | 'L' | 'dose' | 'rouleau' | 'unite' | 'm'
    bulk_worthy: bool,        # vaut le coup d'être stocké
    hard_constraints: list,   # ex. ['type in (silice, agglo_charbon)']
    threshold_good, threshold_stock,   # cf. §4
)

# --- Relevé de prix ---
PriceObservation(
    id, observed_at, store_id, basket_item_id,
    product_label,            # libellé exact du drive
    pack_size, pack_unit,     # 500, 'g'  /  6, 'L'  /  24, 'rouleau'
    price_eur,                # prix payé pour le pack
    unit_price,               # CALCULÉ, jamais saisi
    weight_basis,             # 'brut' | 'net_egoutte'  → sinon incomparable
    mechanic,                 # None | 'second_-30' | 'second_-50' | 'second_-60' | 'lot' | '3_pour_2'
    effective_unit_price,     # prix moyen sur la quantité RÉELLEMENT achetée (cf. §5)
    loyalty_pct,              # avantage carte
    loyalty_valid_until,      # ⚠ date de RETRAIT, pas de commande
    valid_from, valid_until,
    source,                   # 'drive' | 'catalogue' | 'aggregator'
    verified_in_drive: bool,  # SEUL critère pour être actionnable
    source_url,
)
```

**Invariant central :** `verified_in_drive == False` ⇒ l'observation peut être stockée comme *piste* mais ne peut pas entrer dans un compte rendu comme offre.

---

## 4. Seuils de décision (règles métier calibrées)

À externaliser dans un YAML/JSON versionné, pas en dur.

| Poste | Seuil « bon » | Seuil « stocker » | Note |
|---|---|---|---|
| Lait demi-écrémé | 0,80–0,94 €/L | < 0,80 €/L | > 1 €/L → attendre. Tendance baissière, pas d'urgence. |
| Légumineuses sèches | < 2,50 €/kg | | Réf. gros 1,65–1,90 €/kg HT |
| Conserves de poisson | < 10 €/kg | promo −50 % | Pénurie sardines → stocker dès que correct. Repère atteignable : 11,07 €/kg |
| Litière silice | < 1,30 €/L | | Contrainte de type prioritaire |
| Litière agglo charbon | < 0,92 €/L | | Référence : U 5 L à 4,59 € |
| Croquettes milieu gamme | < 3,20 €/kg | < 3,00 €/kg | Meilleur réel vérifié : 3,07 €/kg |
| Lessive | < 0,20 €/dose ou < 2 €/L | | Meilleur : 0,094 €/lavage net |
| Papier toilette | < 0,25 €/rouleau | | Action 0,207 €/rl en prix permanent, imbattable |
| Liquide vaisselle | < 2 €/L | | |
| Steak haché | 15,50–16,20 €/kg | | > 18 €/kg → non |
| Emmental râpé | < 8,00 €/kg | | Cote de gros 7,89 €/kg |
| Mozzarella | < 7 €/kg | | |
| Comté | 17,50–21,90 €/kg = **prix normal**, pas une promo | | |
| Tomate | < 2 €/kg | | Cours de gros 3,44 €/kg, +44 % sur 11 mois |
| Vinaigre | bidon 0,80–1,10 €/L | | Toujours conseiller le bidon 5 L + pulvérisateur plutôt que le spray (3 à 5× moins cher) |

---

## 5. Anti-pièges — la valeur ajoutée du code

Ce sont les règles qui ont le plus rapporté pendant l'expérimentation. Chacune est un **test unitaire évident**.

```python
def validate(obs: PriceObservation) -> Verdict:
    # P1 — Ratio exactement 2,00 → lot ou « 2e à -50 % », jamais une remise de moitié.
    if obs.regular_price and abs(obs.regular_price / obs.price_eur - 2.0) < 0.02:
        return REJECT("prix habituel au double exact = mécanique 2e article")

    # P2 — Ratio > 2,4 → donnée aberrante de l'agrégateur ; le « promo » est en fait le prix normal.
    if obs.regular_price and obs.regular_price / obs.price_eur > 2.4:
        return REJECT("ratio aberrant, agrégateur peu fiable")

    # P3 — Mécanique « Nᵉ à -X% » : le prix pertinent est la MOYENNE sur la quantité achetée.
    #      Contre-exemple vécu : 4,43 € le 1er sac + 3,10 € le 2e = 0,377 €/L, pas 0,31 €/L.
    if obs.mechanic and obs.mechanic.startswith("second_"):
        obs.effective_unit_price = mean_over_required_qty(obs)

    # P4 — Grammage absent → interdiction de calculer un €/kg.
    if obs.pack_size is None:
        return FLAG("format non précisé — ne pas annoncer de prix au kilo")

    # P5 — Poids brut vs net égoutté (conserves) : incomparable sans conversion.
    #      Leclerc annonce en brut, Intermarché en net égoutté.
    if obs.category == "conserve" and obs.weight_basis is None:
        return FLAG("base de poids inconnue")

    # P6 — Avantage carte : ne s'applique que si la date de RETRAIT est dans la fenêtre.
    if obs.loyalty_pct and pickup_date > obs.loyalty_valid_until:
        obs.loyalty_pct = 0

    # P7 — Petits formats en promo : convertir au kilo AVANT de conclure.
    #      Vécu : 125 g à -30 % = 11,52 €/kg contre un 500 g plein tarif à 6,78 €/kg.
    return compare_on(obs.effective_unit_price)
```

**P8 — Coût du détour.** Une économie ne vaut que si elle dépasse le carburant du détour. Règle appliquée : ~2,50 € d'économie pour ~25 km de détour ≈ le prix du carburant → ne vaut pas le déplacement. À paramétrer (`cost_per_km`).

---

## 6. Sources — ce qui marche, ce qui ne marche pas

**Fiables (à implémenter) :**
- `promocatalogues.fr/offres/<produit>/` — **avec le slash final**, de loin le plus productif
- `promocatalogues.fr/magasins/<enseigne>/catalogues-promotions` — titres et dates des opérations en cours (utile pour le calendrier même sans détail produit)
- `vos-promos.fr/produits/<produit>`, `bonial.fr/Promos/<produit>`
- `action.com/fr-fr/c/...` — prix permanents, imbattables en droguerie
- `zooplus.fr`, `maxizoo.fr` — prix de référence animalerie
- `agidra.com` (légumes secs en gros), `foodomarket.com` (cours Rungis)
- `intermarche.com/enseigne/bons-plans/bon-achat` — dit clairement s'il y a une opération en cours

**À ne pas retenter :** `anti-crise.fr` · `e.leclerc` (403) · `auchan.fr` et `coursesu.com` sans magasin sélectionné (prix masqué) · `supermarche.tv`, `lineaires.com`, `web-agri.fr` (robots.txt) · `cataloguemate.fr`, `kimbino.fr`, `icatalogue.fr` (pages de navigation sans détail) · Amazon.fr (aucun prix structuré).

**Drives — URL qui fonctionnent :**
```
Leclerc  recherche : https://fd7-courses.leclercdrive.fr/magasin-173501-173501-Pleumeleuc/recherche.aspx?TexteRecherche={q}
Leclerc  panier    : https://fd7-courses.leclercdrive.fr/magasin-173501-173501-Pleumeleuc/detail-panier.aspx
Intermarché        : https://www.intermarche.com/recherche/{q}     (⚠ /drive/<code> → 404)
Courses U          : https://www.coursesu.com/recherche?q={q}  ·  panier : /panier
```

**Calendrier :** gros basculement des catalogues le 1er du mois, fin des opérations le samedi soir. Les catalogues Leclerc de la semaine suivante n'arrivent chez les agrégateurs que quelques jours avant — un run du dimanche ne verra pas le catalogue du mardi.

---

## 7. Automatisation du drive — ce qu'on a appris à la dure

C'est la partie la plus fragile. Si le projet doit être robuste, c'est ici qu'il faut investir.

**Contraintes fonctionnelles posées par Thomas :**
- Le code **remplit le panier et s'arrête là**. Le choix du créneau et le paiement restent humains.
- **Aucun identifiant stocké.** Pas de `.env` avec des mots de passe, pas de saisie de mot de passe automatisée. Demande explicitement écartée pendant l'expérimentation. Le modèle retenu : l'humain se connecte lui-même, la session persiste par cookies pendant des semaines.

**Pièges techniques rencontrés (Leclerc Drive) :**
- Se connecter sur `www.leclercdrive.fr` **ne suffit pas** : il faut cliquer « Commencer mes courses » pour propager la session vers `fd7-courses.leclercdrive.fr`. Ce sous-domaine doit être autorisé séparément.
- Sur la page panier : les clics par référence d'élément échouent silencieusement ; les clics par coordonnées échouent aussi de façon intermittente. Chaque suppression ouvre une modale « Confirmation suppression de produit » à valider.
- Les pages « Promotions » et « Nos bons plans » sont **composées d'images sans texte** → illisibles par lecture de page. Il faut passer par des recherches produit par produit.
- Le site est lent : `Page.captureScreenshot` et l'injection de script partent régulièrement en timeout.
- Chez Intermarché : se connecter à un autre magasin **bascule le magasin actif** de tout le compte.

**Recommandation d'architecture :** ne pas construire sur du pilotage d'interface. Préférer, par ordre de robustesse :
1. **Reverse-engineering des appels XHR** du drive (recherche produit, ajout panier) une fois la session ouverte — c'est du JSON, c'est stable, c'est testable.
2. À défaut, **Playwright** avec un profil persistant que l'humain a authentifié à la main, et une couche de retry/idempotence sur chaque action panier (vérifier l'état du panier après chaque mutation plutôt que de faire confiance au clic).
3. Le pilotage par coordonnées d'écran est à considérer comme un dernier recours non fiable.

---

## 8. Architecture proposée

```
veille-courses/
├── config/
│   ├── stores.yaml           # référentiel magasins, corridors, affectations
│   ├── basket.yaml           # panier type + contraintes dures (litière…)
│   ├── thresholds.yaml       # §4
│   └── sources.yaml          # §6, avec allow/deny list
├── src/
│   ├── ingest/               # collecteurs par source (agrégateurs)
│   ├── drive/                # clients par enseigne : search(), cart_add(), cart_state()
│   │   ├── leclerc.py
│   │   ├── intermarche.py
│   │   └── coursesu.py
│   ├── normalize.py          # unit_price, weight_basis, mécaniques → effective_unit_price
│   ├── validate.py           # P1…P8, 100 % testé
│   ├── assign.py             # offre → magasin → personne (corridor + coût de détour)
│   ├── ledger.py             # historique, records, détection de « vrai » record
│   └── report.py             # markdown + bloc WhatsApp + e-mail
├── tests/
│   └── test_traps.py         # les contre-exemples réels du §5 comme fixtures
└── data/
    └── observations.sqlite   # ou DuckDB
```

**Pipeline hebdomadaire :**
```
collect (agrégateurs) → normalize → validate → shortlist
   → verify_in_drive (le seul filtre qui compte)
   → assign (corridor + personne)
   → report (par magasin) → e-mail + bloc WhatsApp
   → ledger.update (records, tendances)
```

**Sortie attendue :** une liste **par magasin**, prête à remplir dans le drive — pas un classement par catégorie de produit. Plus un bloc copier-coller pour WhatsApp en tête. Destinataires : houe.thomas@gmail.com et charlotte.barbe.ergo@gmail.com. Vendredi 18h.

---

## 9. Backlog suggéré

| Priorité | Lot | Pourquoi |
|---|---|---|
| **P0** | `normalize.py` + `validate.py` + tests sur les contre-exemples réels | C'est là qu'est toute la valeur ; testable hors réseau ; réutilisable quelle que soit la suite |
| **P0** | `config/*.yaml` : stores, basket, thresholds | Aujourd'hui cette connaissance vit dans de la mémoire non structurée |
| **P1** | Client drive Leclerc en XHR (`search`, `cart_state`) | Le plus utilisé, le plus pénible à piloter à la main |
| **P1** | `ledger` + détection de record | Évite de re-signaler une offre médiocre comme une trouvaille |
| **P2** | `assign.py` avec coût de détour paramétrable | Automatise l'arbitrage qui est fait à la main aujourd'hui |
| **P2** | `report.py` (markdown + WhatsApp + mail) | |
| **P3** | Clients Intermarché et Courses U | |
| **P3** | Liste de courses partagée (saisie vocale) alimentant `basket` | Existe en artifact, à reconnecter proprement |

---

## 10. Points ouverts

- **Litière** : trois semaines consécutives sans aucune promo conforme (silice/charbon) sur les corridors. Décision actée : arrêter d'attendre, acheter au prix courant (U 5 L charbon actif à 0,92 €/L). Le code devrait savoir **conclure à l'absence d'offre** plutôt que de proposer un pis-aller non conforme.
- **Drives restant à connecter**, par utilité : Super U Breteil ou L'Hermitage (les offres U ne sont accessibles autrement) → E.Leclerc Ploufragan → Intermarché Trémuson/Trégueux → Intermarché Hyper Lamballe.
- **Fruits et légumes** : 8 articles du panier gagnent à sortir du périmètre drive (Grand Frais Trégueux, marché). À traiter comme une catégorie à part, sans veille prix.
- Aldi, Lidl, Netto, Action **n'ont pas de drive** : rien à automatiser, ces achats restent en magasin. La veille sur ces enseignes reste utile mais produit une liste papier.

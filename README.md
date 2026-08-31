# Veille courses & remplissage de drive

Foyer Thomas & Charlotte — Montauban-de-Bretagne (35360).

Implémentation de la spec [`docs/SPEC-veille-courses.md`](docs/SPEC-veille-courses.md), issue de trois semaines
d'expérimentation manuelle (29–31 août 2026).

> Ce n'est pas une veille promo. C'est un **optimiseur d'affectation panier →
> magasin sous contrainte de géographie**, alimenté par une veille prix.

---

## Les trois constats qui pilotent tout le code

| | Constat | Où il est implémenté |
|---|---|---|
| **C1** | Le catalogue n'est pas l'assortiment du drive. Les prix de prospectus sont des prix **magasin**. | `src/drive/verify.py` — seule `verified_in_drive == True` rend une observation actionnable. Quand le produit est retrouvé, **c'est le prix du drive qui écrase celui du prospectus**. |
| **C2** | Les agrégateurs publient des prix faux de façon systématique et **prévisible**. | `src/validate.py` — règles P1…P7, déterministes, sans réseau, testées une par une. |
| **C3** | Un prix affiché n'est presque jamais comparable d'une enseigne à l'autre. | `src/units.py` + `src/normalize.py` — `unit_price` et `effective_unit_price` sont **calculés, jamais saisis**. |

Trois invariants en découlent, et chacun a son test :

* une observation non vérifiée en drive est une **piste**, jamais une offre ;
* **Carrefour et Auchan** sont filtrés en entrée *et* assertés absents en sortie ;
* une **contrainte dure** (litière silice ou charbon actif, vinaigre en bidon)
  s'évalue **avant** le prix : un produit non conforme n'est pas une affaire
  bon marché, c'est un hors-sujet.

---

## Installation

```bash
pip install -r requirements.txt          # PyYAML + requests, c'est tout

# Optionnel, uniquement pour piloter les drives :
pip install playwright && playwright install chromium
```

Le cœur du projet (`normalize`, `validate`, `assign`, `report`) n'a **aucune
dépendance réseau** et tourne sans Playwright.

## Prise en main

```bash
# Run complet (collecte → vérification drive → rapport)
python -m src.cli run --out data/reports

# Run hors ligne à partir de relevés saisis à la main
python -m src.cli run --offline --no-drive --manual data/manual.example.json

# Vérifier un prix vu en rayon, tout de suite
python -m src.cli check --item litiere_chat --store superu_breteil \
    --label "Litière agglomérante au charbon actif 5 L" --price 4.59

# Le piège du « 2ᵉ à -30 % », démonté
python -m src.cli check --item litiere_chat --store leclerc_pleumeleuc \
    --label "Litière silice 10 L" --price 4.43 --mechanic second_-30
#   → Prix normalisé : 0,443 €/L
#   → Prix effectif  : 0,377 €/L  (moyenne sur 2 — c'est CE prix qui compte)

# Historique et tendance d'un poste
python -m src.cli history --item lait_demi_ecreme

# Drive : se connecter soi-même, puis chercher / remplir
python -m src.cli login  --banner leclerc
python -m src.cli search --store leclerc_pleumeleuc --query "lait demi-écrémé"
python -m src.cli fill   --stores leclerc_pleumeleuc --commit
```

Tests : `python -m pytest` (132 tests, aucun accès réseau).

---

## Organisation

```
config/          toute la connaissance calibrée, versionnée, jamais en dur
  stores.yaml      magasins, corridors, affectations, detour_km, exclusions
  basket.yaml      panier type, contraintes dures, règles d'attributs
  thresholds.yaml  §4 — seuils bon / stocker / plafond + cost_per_km
  sources.yaml     §6 — allow/deny-list, appliquée AVANT toute requête

src/
  units.py       familles d'unités, lecture des conditionnements (« 6x1L »)
  normalize.py   prix unitaire, mécaniques, avantage carte, poids égoutté
  validate.py    P1…P8 + contraintes dures + exclusions  ← le cœur de valeur
  assign.py      offre → magasin → personne, arbitrage du détour
  ledger.py      SQLite : historique, records, tendances
  report.py      markdown par magasin + bloc WhatsApp + .eml
  pipeline.py    l'enchaînement complet
  cli.py         run / check / login / search / history / fill
  ingest/        collecteurs d'agrégateurs (produisent des PISTES)
  drive/         clients par enseigne : search / cart_state / cart_add

tests/           les contre-exemples réels de la spec, en fixtures
data/            base SQLite, cache HTTP, rapports (non versionnés)
```

### Le pipeline

```
collect (agrégateurs) → normalize → validate → shortlist
   → verify_in_drive (le seul filtre qui compte)
   → assign (corridor + personne)
   → report (par magasin) → e-mail + bloc WhatsApp
   → ledger.update (records, tendances)
```

`shortlist` tourne **avant** tout accès au drive : inutile d'aller vérifier en
ligne ce qu'on sait déjà faux ou non conforme.

---

## Les anti-pièges (§5), et ce qu'ils coûtaient

| Règle | Piège | Comportement |
|---|---|---|
| **P1** | Prix habituel au double exact → c'est un lot ou un « 2ᵉ à −50 % » | rejet |
| **P2** | Ratio > 2,4 → donnée aberrante de l'agrégateur | rejet |
| **P3** | « Nᵉ à −X % » : 4,43 € + 3,10 € = **0,377 €/L**, pas 0,31 €/L | moyenne sur la quantité réellement achetée |
| **P4** | Grammage absent | interdiction de calculer un €/kg |
| **P5** | Poids brut (Leclerc) vs net égoutté (Intermarché) | conversion si le ratio est connu, sinon signalement |
| **P6** | Avantage carte lié à la date de **retrait** | annulé hors fenêtre |
| **P7** | 125 g à −30 % = 11,52 €/kg vs 500 g plein tarif à 6,78 €/kg | comparaison sur le normalisé, promo trompeuse signalée |
| **P8** | 2,50 € d'économie pour 25 km de détour ≈ le carburant | `cost_per_km`, magasin écarté si le gain net ne suit pas |

Un article dont le magasin est écarté pour cause de détour n'est **pas**
annoncé comme introuvable : il ressort sous « Conforme, mais pas cette
semaine ». Confondre les deux ferait passer un arbitrage de trajet pour un
échec de veille.

---

## Automatisation du drive — ce qui est garanti, et ce qui ne l'est pas

**Deux contraintes fonctionnelles, tenues :**

* le code **remplit le panier et s'arrête là** — créneau et paiement restent
  humains ;
* **aucun identifiant n'est stocké**, nulle part. `python -m src.cli login`
  ouvre un navigateur, *vous* vous connectez, les cookies restent dans un
  profil Chromium local (`~/.veille-courses/profiles/`) et durent des semaines.

**Ce qui est robuste :** chaque mutation du panier est **vérifiée par
relecture** (`cart_state`) et retentée. Les clics qui échouent silencieusement
— le comportement constaté sur Leclerc Drive — sont rattrapés ; ce cas précis
a son test.

**Constat du 2026-08-31 — Leclerc Drive bloque les navigateurs pilotés.**
Playwright, même sur un profil authentifié à la main, déclenche « Accès
temporairement restreint — quelque chose dans le comportement du navigateur
nous a intrigué ». Ce n'est pas un bug du code : c'est un contrôle d'accès
délibéré du site. On ne cherche pas à le déguiser — ce serait une course sans
fin, et ce n'est pas à nous d'en décider. La réponse est de **retirer
l'automate du chemin** : voir `parse-page` ci-dessous, qui lit une page que
vous avez enregistrée vous-même depuis votre navigateur habituel. L'invariant
C1 tient toujours, puisque la page vient bien du drive.

**Ce qui reste fragile, en toute franchise :** les sélecteurs DOM des trois
drives et les gabarits des agrégateurs sont écrits d'après la spec et n'ont pas
pu être confrontés aux sites en direct depuis cet environnement. Ils sont donc
**isolés en un seul endroit par client** (la fonction `search` / `cart_state`)
et les collecteurs **journalisent bruyamment** quand ils n'extraient rien d'une
page non vide — un rapport vide qui ressemble à « pas de promo » est pire
qu'une erreur. La voie recommandée reste le **XHR** (`parse_search_xhr` est déjà
là et testé) dès que les endpoints auront été relevés une fois, session ouverte.

### La voie qui marche partout : `parse-page`

Vous naviguez dans le drive **normalement**, dans votre navigateur habituel.
Sur la page de résultats, `Ctrl+S` → « Page web complète ». Puis :

```bash
python -m src.cli parse-page --store leclerc_pleumeleuc --file "lait.html"
python -m src.cli run --no-drive --manual data/manual.json
```

Aucun pilotage, donc rien à détecter, et rien qui casse quand le site change
de pare-feu applicatif. Le module essaie trois lectures dans l'ordre : JSON-LD,
JSON embarqué dans la page, puis les blocs HTML. Ajoutez `--append` pour
empiler plusieurs recherches avant de lancer le run.

`--manual` accepte aussi un JSON saisi entièrement à la main : c'est la
dernière voie de secours, et elle donne exactement le même rapport.

**Pièges déjà encodés :** propagation de session Leclerc vers
`fd7-courses` (« Commencer mes courses »), modale de confirmation à la
suppression, bascule du magasin actif chez Intermarché (le client **refuse** de
travailler sur le mauvais magasin), prix masqués sur `coursesu.com` sans
magasin sélectionné (traité comme session incomplète, pas comme produit absent).

---

## Faire évoluer les réglages

Rien de calibré ne se touche dans le code :

* un magasin, un corridor, un `detour_km` → `config/stores.yaml` ;
* un seuil, le `cost_per_km`, le `min_net_gain_eur` → `config/thresholds.yaml` ;
* un article, une contrainte dure, un mot-clé → `config/basket.yaml` ;
* une source qui marche ou qui ne marche plus → `config/sources.yaml`.

Un seuil peut dépendre d'un attribut : la litière se juge à 1,30 €/L en silice
et 0,92 €/L en agglomérante charbon, via `by_attribute` — et un seuil exprimé
dans une unité incompatible avec le relevé ne note rien plutôt que de noter
faux.

---

## Reste à faire

* Relever une fois les endpoints XHR des drives, session ouverte, et basculer
  `search` / `cart_add` dessus (§7, voie 1) — les sélecteurs DOM deviendront le
  filet de sécurité.
* Connecter les drives par ordre d'utilité : Super U Breteil ou L'Hermitage,
  puis E.Leclerc Ploufragan, Intermarché Trémuson/Trégueux, Hyper Lamballe.
* Envoi automatique du vendredi 18 h : le `.eml` est généré
  (`run --eml courses.eml`), l'envoi reste manuel tant qu'aucun identifiant
  n'est stocké.
* Reconnecter la liste de courses partagée (saisie vocale) sur `basket.yaml`.

# Export vers Home Assistant — onglet « Courses »

La veille du vendredi écrit ici **`veille_courses.json`** ; le Green de la
maison le lit toutes les heures (capteur REST sur l'API GitHub, jeton en
lecture seule) et en tire l'onglet « Courses » et les notifications.

Avant chaque push : `python -m src.export_ha data/ha/veille_courses.json`
doit répondre `OK`. Le fichier est **réécrit en entier** chaque semaine.

## Contrat (version 1)

| Clé | Contenu |
|---|---|
| `version` | `1` |
| `genere_le` | date-heure ISO du relevé (`2026-10-30T18:40:00+01:00`) |
| `semaine_du` | lundi de la semaine visée (`AAAA-MM-JJ`) |
| `resume` | une ou deux phrases, comme l'ouverture du mail |
| `enseignes` | exactement `leclerc`, `intermarche`, `superu` → nom du magasin |
| `comparatif` | une ligne par produit de la liste partagée (sinon du panier type) |
| `opportunites` | les offres qui valent l'arrêt, toutes enseignes (Lidl, Aldi… compris) |

**Ligne de `comparatif`** : `produit`, `format` (texte), `unite` (`kg`, `L`,
`unite`, `dose`, `rouleau`, `lavage`, `piece`), puis une cellule par enseigne
— `null` si le prix n'a pas été trouvé, jamais inventée :
`{"prix": 5.08, "pu": 0.85, "promo": true, "fin": "2026-11-01", "libelle": "…"}`
(`pu` = prix ramené à l'unité, `null` si le grammage manque ; `fin` = fin de
la promo, `null` hors promo). `moins_cher` = l'enseigne au `pu` le plus bas
(ou `null`), `record_pu` = meilleur prix unitaire jamais relevé (registre).

**`opportunites`** : `titre`, `magasin`, `qui` (`maison`, `charlotte`,
`thomas`), `prix`, `pu`, `unite`, `fin`, `niveau` (`record`, `bon`,
`a_surveiller`), `alerte`, `note`. **`alerte: true`** déclenche une
notification sur les téléphones : la réserver aux records et aux offres qui
valent vraiment l'arrêt (3 à 5 par semaine au plus). Le Green rappelle aussi,
la veille au matin, les offres en alerte qui se terminent le lendemain.

Mêmes règles que la veille : pas de Carrefour ni d'Auchan, aucune donnée
personnelle, aucun prix sans source. Taille max 60 ko.

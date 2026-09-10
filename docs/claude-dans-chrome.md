# Faire relever les prix par Claude dans Chrome

L'extension navigue dans VOTRE navigateur : les drives la laissent passer là
où ils bloquent un navigateur piloté. Mais on ne lui demande QUE ce qu'elle
fait bien — lire — et jamais ce que le pipeline fait mieux : calculer les
prix normalisés, appliquer les règles P1…P8, tenir l'historique. La leçon
vient de l'expérimentation d'origine : c'est en calculant de tête que
l'assistant avait annoncé un « bon prix » sardines sur une base brut/égoutté
non comparable.

## Le circuit

0. `python -m src.cli open-tabs --store leclerc_pleumeleuc --bulk` ouvre les
   recherches comme onglets dans VOTRE navigateur (celui déjà connecté ;
   aucun cookie n'est copié — les drives bloquent l'automatisation, pas votre
   navigateur). `--script ouvrir.bat` écrit un fichier rejouable à la place.
1. `python -m src.cli shortlist` → la liste des recherches à faire, avec les URL.
2. Coller à Claude dans Chrome le prompt ci-dessous, complété avec ces URL.
3. Copier sa réponse (Ctrl+C sur le bloc JSON), puis :
   `python -m src.cli paste` — la commande lit le presse-papiers, valide
   chaque relevé (magasin et article connus, prix présent) et l'ajoute à
   `data/manual.json`. Une extension ne peut pas enregistrer de fichier ;
   elle n'en a pas besoin.
4. `python -m src.cli run --no-drive --manual data/manual.json`

L'étape 2 remplace le Ctrl+S — c'est le même contrat : des relevés bruts,
`verified_in_drive: true` parce qu'ils viennent bien du drive, et AUCUN
calcul fait en chemin.

## Le prompt à coller dans Claude dans Chrome

> Ouvre chacune de ces pages de recherche de drive (je suis connecté, le
> magasin est déjà sélectionné) : `<coller les URL de shortlist>`.
>
> Pour chaque produit affiché qui correspond à la recherche, relève SANS RIEN
> CALCULER :
> - le libellé exact, tel qu'affiché, grammage compris ;
> - le prix affiché du pack (pas le prix au litre/kilo, pas le prix barré) ;
> - le prix barré s'il y en a un ;
> - la mécanique s'il y en a une (« 2e à -30% », « lot de 2 », « 3 pour 2 ») ;
> - pour les conserves : la mention « net égoutté » ou « poids net » si visible ;
> - s'il est indisponible.
>
> Ne convertis aucune unité, ne compare rien, ne juge rien : un programme le
> fait derrière. Rends UNIQUEMENT un tableau JSON, un objet par produit :
>
> ```json
> [{
>   "store_id": "<je te le donne : leclerc_pleumeleuc | hyperu_yffiniac | intermarche_montauban>",
>   "basket_item_id": "<je te le donne, ex. litiere_chat>",
>   "product_label": "Litière Tranquille Cristale charbon actif 4 L",
>   "price_eur": 5.99,
>   "regular_price": null,
>   "mechanic": null,
>   "weight_basis": null,
>   "available": true,
>   "verified_in_drive": true,
>   "source": "drive"
> }]
> ```
>
> `mechanic` : `second_-30`, `second_-50`, `second_-60`, `lot`, `3_pour_2` ou
> null. `weight_basis` : `net_egoutte`, `brut` ou null. N'invente aucun champ,
> n'omets aucun produit affiché, même hors budget : le tri n'est pas ton rôle.

## Ce qui reste vrai quoi qu'il arrive

- Les relevés de l'extension passent par les MÊMES règles que tout le reste :
  un prix incohérent part en « à vérifier », une litière au type inconnu ne
  devient jamais une offre.
- Le remplissage du panier reste manuel — l'expérimentation a tranché :
  « c'est plus rapide à la main que par moi de toute façon ».
- Budget d'usage : une session de relevé coûte des actions d'extension ;
  le Ctrl+S + `parse-page` reste la voie gratuite et déterministe. Les deux
  produisent exactement le même rapport, choisissez selon l'humeur du vendredi.


## Lever les doutes ciblés

Quand un run laisse des relevés incertains (base de poids absente, format non
précisé, prix incohérent, produit non confirmé), inutile de tout refaire :

```bash
python -m src.cli review --manual data/manual.json --prompt
```

La commande liste UNIQUEMENT les doutes, avec l'URL de la fiche produit quand
elle est connue, et un prompt prêt à coller. L'extension ouvre ces quelques
fiches — la fiche produit est propre et structurée, là où la liste tronque —
relève le détail exact, et `paste` réabsorbe le JSON corrigé. On ne dépense la
lecture par extension que sur ce qui doute, jamais sur tout le panier.


## Zéro Ctrl+S : SingleFile auto-save + Companion

SingleFile sait tout enregistrer seul, ce qui supprime le dernier geste manuel :

1. Dans les options SingleFile → **Auto-sauvegarde** : cocher « auto-sauvegarder
   après le chargement de la page ». Passer le **délai après chargement à 3–5 s**
   (les drives sont lents : 1 s ne suffit pas à charger les prix).
2. Cocher « sauvegarder la page avec SingleFile Companion » et installer le
   Companion : il permet d'écrire ailleurs que dans Téléchargements — pointez-le
   sur un dossier `captures`. Installation (voir
   https://github.com/gildas-lormeau/single-file-companion) :
   - installer Node.js si absent ;
   - récupérer le dépôt (bouton « Code » → Download ZIP, ou `git clone`) ;
   - lancer le script d'installation fourni pour votre système (il enregistre
     l'hôte de messagerie native que l'extension appelle) ;
   - dans les options SingleFile, régler le dossier de destination du Companion
     sur le `captures` de ce projet (chemin absolu).
   Une fois en place, chaque page auto-sauvegardée atterrit directement dans
   `captures`, sans passer par Téléchargements.
3. Le vendredi : `python -m src.cli open-tabs --store … --bulk`. IMPORTANT :
   avec beaucoup d'onglets, l'auto-save « après chargement » RATE les onglets
   en arrière-plan (Chrome les gèle et ils ne déclenchent jamais leur save).
   Utilisez plutôt le bouton SingleFile **« Enregistrer tous les onglets »**
   (clic droit sur l'icône) : il réveille chaque onglet et les sauve tous dans
   `captures`. L'auto-save reste pratique pour une page unique.
4. Un seul dossier, magasins mêlés : l'auto-détection s'en charge —

```
python -m src.cli parse-page --dir captures
python -m src.cli compare --manual data/manual.json
```

`parse-page --dir` **sans `--store`** reconnaît chaque page (Leclerc, Intermarché,
Hyper U) à son contenu et la range toute seule. Plus de tri, plus de copier-coller.

Sans le Companion, les pages tombent dans Téléchargements — ça marche pareil :
pointez simplement `parse-page --dir` sur votre dossier Téléchargements.


## Lidl et Aldi : le flyer se lit à l'œil

Les catalogues Lidl et Aldi sont des FLYERS — des images de pages scannées
(URL en `.../view/flyer/...`). Zéro texte, donc zéro prix lisible par
parse-page : le HTML est vide de données.

C'est le seul cas où la vision de Claude dans Chrome apporte ce que le code ne
peut pas. Ouvrez le flyer dans le navigateur et donnez ce prompt à l'extension :

> Voici le catalogue Lidl de la semaine en images. Relève les produits qui
> correspondent à mon panier (lait, légumineuses, conserves de poisson, café,
> lessive, papier toilette, litière…). Pour chacun, SANS CALCULER : libellé
> exact, prix affiché, grammage, mécanique promo éventuelle. Rends un tableau
> JSON : store_id "lidl_langueux" (ou "aldi_tregueux"), basket_item_id,
> product_label, price_eur, pack_size, pack_unit, source "catalogue".

Puis `python -m src.cli paste`. La normalisation, les seuils et l'affectation
s'appliquent ensuite comme pour n'importe quel relevé — ces prix sortiront en
liste papier (Lidl et Aldi n'ont pas de drive).

Alternative sans extension : les agrégateurs, via `run --collect`, qui récupèrent
les catalogues Lidl/Aldi sous forme structurée quand ils sont disponibles.

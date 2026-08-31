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

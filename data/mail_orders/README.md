# Relevés de commande (veille des mails)

La routine hebdo (cloud) lit Gmail, parse chaque confirmation de commande drive
et dépose ici **un fichier JSON par commande** — uniquement produits, prix et
formats (aucune donnée personnelle : ni adresse, ni e-mail).

Sur le PC, après `git pull` :

```
python -m src.cli sync-orders
```

verse ces relevés dans l'historique local (`data/observations.sqlite`), ce qui
fait grandir la mémoire des habitudes et alimente la détection des promos.
Idempotent : resynchroniser ne double rien.

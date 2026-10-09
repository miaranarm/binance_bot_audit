# Binance Public Bot Audit

Collecte et classement des stratégies publiquement visibles dans le Binance Bot Marketplace. Le projet est en **lecture seule** : aucune clé API, aucun ordre et aucune copie automatique de bot.

## Source de vérité

- **Rapport principal :** [`results/current_multicriteria.csv`](results/current_multicriteria.csv)
- Résumé court : `results/current_multicriteria_summary.txt`
- Exhaustivité du scan : `results/scan_meta.json` (comparaison du nombre collecté avec le total annoncé par Binance pour chaque catégorie)
- Recensement des familles : `results/type_census.json`
- Contrôle des estimations : `results/grid_profit_validation.json`
- Historique compact : `results/history/YYYY/MM/DD/*.csv.gz`

Le rapport principal est généré par `src/audit_all.py`, contrôlé par `src/validate_results.py`, puis finalisé par `src/build_multicriteria.py`. Le fichier final conserve les 29 colonnes définies dans ce script.

## Critères et interprétation

Le périmètre de collecte est le Bot Marketplace public Binance. Le scanner conserve les stratégies ayant un `strategyId`, un symbole et un levier connu inférieur ou égal à 1 ; les stratégies dont le levier est inconnu hors Spot Grid ne sont pas présumées sans levier. Après cette règle de périmètre, **aucune stratégie ne doit être retirée selon son ancienneté, le prix hors plage ou le profit par grille après frais**. Le générateur final déduplique les `strategyId`, conserve les lignes du périmètre collecté et ne filtre pas sur les métriques de performance.

Pendant la phase de fiabilisation, le score et le rang restent volontairement vides : ils ne doivent pas masquer ou réordonner la liste complète. La liste est triée par `strategyId` pour assurer un ordre stable, pas par performance.

### Données officielles et estimations

- `roi (fourni par Binance)` conserve le ROI publié par Binance.
- `roi (calculé)` est calculé comme PNL / investissement minimum × 100. L'investissement minimum peut différer du capital réellement investi : ce champ est un indicateur de comparaison, pas une reproduction garantie du ROI Binance.
- `gridProfit` est exact uniquement quand une donnée publique Binance suffisamment explicite permet de l'établir. Sinon, il reste vide dans la colonne principale et le point central estimé est accompagné d'une fourchette basse/centrale/haute lorsqu'elle peut être calculée.
- `gridProfitTotalProfitRatio` est calculé dès que le Total Profit est non nul. Quand le Grid Profit est exact, le ratio est numérique ; quand le Grid Profit est estimé, le ratio central est préfixé par `≈` pour signaler l'estimation, tandis que `gridProfit` reste vide. Cela conserve les 29 colonnes actuelles tout en distinguant une estimation d'une valeur exacte.
- Aucun plafond arbitraire de 100 % ou 500 % n'est appliqué au ratio. Un ratio supérieur à 100 % est conservé et doit être interprété en regard du signe et de la base comptable du Total Profit, du Floating Profit et des sources Binance. Si le Total Profit est nul, le ratio est indéfini et reste vide.
- `floatingProfit` reste vide dès que le Grid Profit n'est pas exact. Le résidu entre un profit total et un Grid Profit estimé n'est pas un Floating Profit fiable.
- Le nombre de transactions ne suffit pas à retrouver le profit exact de chaque cycle. Une reconstruction fondée sur la géométrie de la grille demeure une estimation et ne doit jamais être présentée comme une donnée officielle.
- `profitPerGridAfterFees` utilise la formule documentée de la grille et les frais Spot de référence. Les frais effectifs peuvent varier selon la paire, le niveau VIP et les réductions de frais. Cette valeur ne sert pas de filtre : les valeurs faibles ou négatives restent dans la liste lorsqu'elles sont calculables.

Une collecte n'est considérée exhaustive que si `scan_meta.json` indique `collection_complete: true`. Cela signifie que le nombre de lignes récupérées correspond au total annoncé par les endpoints publics interrogés; cela ne prouve pas que Binance expose toutes les familles de bots existantes dans une API publique.

## Automatisation

Le workflow `.github/workflows/audit.yml` effectue une collecte horaire, conserve les captures réseau comme artefacts temporaires Actions, valide les métriques et maintient l'historique compact. Les outils dans `tools/` sont des diagnostics manuels destinés à rechercher les champs exacts exposés par Binance; ils ne sont pas une source de données garantie.

## Lancer manuellement

Dans GitHub : **Actions → Binance Public Bot Audit → Run workflow**.

Le projet utilise Python 3.11 et les dépendances listées dans `requirements.txt`.

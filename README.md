# Binance Public Bot Audit

Collecte et classement des stratégies publiquement visibles dans le Binance Bot Marketplace. Le projet est en **lecture seule** : aucune clé API, aucun ordre et aucune copie automatique de bot.

## Source de vérité

- **Rapport principal :** [`results/current_multicriteria.csv`](results/current_multicriteria.csv)
- Résumé court : `results/current_multicriteria_summary.txt`
- Exhaustivité du scan : `results/scan_meta.json` (comparaison du nombre collecté avec le total annoncé par Binance pour chaque catégorie)
- Recensement des familles : `results/type_census.json`
- Contrôle des estimations : `results/grid_profit_validation.json`
- Historique compact : `results/history/YYYY/MM/DD/*.csv.gz`

Le rapport principal est généré par `src/audit_all.py`, contrôlé par `src/validate_results.py`, puis normalisé par `src/finalize_multicriteria.py`. Le fichier final ne contient que les 29 colonnes définies dans le script de finalisation.

## Critères et interprétation

Le filtre final ne conserve que les stratégies ayant un `strategyId`, un symbole négociable et un levier connu inférieur ou égal à 1. Les stratégies à levier inconnu des familles autres que Spot Grid sont exclues plutôt que supposées sans levier.

Le classement donne la priorité au ratio **Grid Profit / Total Profit**, puis au profit par grille après frais, à la position du prix dans la plage, à la durée, à l'activité, au profit moyen par transaction et au drawdown sur 7 jours. Le score est un outil de présélection, pas une garantie de rendement.

### Données officielles et estimations

- `roi (fourni par Binance)` conserve le ROI publié par Binance.
- `roi (calculé)` est calculé comme PNL / investissement minimum × 100. L'investissement minimum peut différer du capital réellement investi : ce champ est un indicateur de comparaison, pas une reproduction garantie du ROI Binance.
- `gridProfit` est exact uniquement quand une donnée publique Binance suffisamment explicite permet de l'établir. Sinon, le point central estimé est accompagné d'une fourchette basse/centrale/haute lorsqu'elle peut être calculée.
- Le nombre de transactions ne suffit pas à retrouver le profit exact de chaque cycle. Une reconstruction fondée sur la géométrie de la grille demeure une estimation et ne doit jamais être présentée comme une donnée officielle.
- `floatingProfit` reste vide lorsqu'il ne peut pas être calculé à partir de valeurs exactes et comparables. Le résidu entre un profit total et un Grid Profit estimé n'est pas un Floating Profit fiable.
- `profitPerGridAfterFees` utilise la formule documentée de la grille et les frais Spot de référence. Les frais effectifs peuvent varier selon la paire, le niveau VIP et les réductions de frais.

Une collecte n'est considérée exhaustive que si `scan_meta.json` indique `collection_complete: true`. Cela signifie que le nombre de lignes récupérées correspond au total annoncé par les endpoints publics interrogés; cela ne prouve pas que Binance expose toutes les familles de bots existantes dans une API publique.

## Automatisation

Le workflow `.github/workflows/audit.yml` effectue une collecte horaire, conserve les captures réseau comme artefacts temporaires Actions, valide les métriques et maintient l'historique compact. Les outils dans `tools/` sont des diagnostics manuels destinés à rechercher les champs exacts exposés par Binance; ils ne sont pas une source de données garantie.

## Lancer manuellement

Dans GitHub : **Actions → Binance Public Bot Audit → Run workflow**.

Le projet utilise Python 3.11 et les dépendances listées dans `requirements.txt`.

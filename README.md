# Binance Public Bot Audit

Recensement des bots publics **Spot Grid** du Binance Bot Marketplace, sans filtre de levier. Projet en **lecture seule** : aucune clé API, aucun ordre, aucune copie de bot.

## Objectifs

1. **Grid Profit** : rechercher la donnée officielle Binance ; à défaut, estimer au plus fidèle le ratio Grid Profit / Total Profit.
2. **Profit par grille après frais** : le calculer de la manière la plus fiable possible.

Pas de classement : les colonnes `rank` et `score` existent mais restent vides. Aucune ligne n'est retirée pour son âge, son prix hors plage ou son profit par grille.

## Fichiers

| Fichier | Rôle |
|---|---|
| `results/current_multicriteria.csv` | **Livrable** : 31 colonnes figées (ni ajout ni suppression), triées par `strategyId` |
| `results/current_multicriteria_summary.txt` | Résumé court |
| `results/scan_meta.json` | Exhaustivité (collecté vs annoncé par catégorie), pages de détail visitées, validation |
| `results/history/YYYY/MM/DD/*.csv.gz` | Brut horaire avec provenance, 30 jours |
| `src/audit_all.py` | Collecte + Grid Profit exact/estimé + profit par grille → CSV brut |
| `src/validate.py` | Contrôles `raw` (provenance) puis `final` (schéma et calculs) |
| `src/build_multicriteria.py` | Réécrit le CSV brut au format final figé de 31 colonnes |
| `src/build_current_price.py` | Prix par paire et extrêmes observés dans l'historique du projet |
| `src/build_grid_setup.py` | Fourchette Spot Grid indicative, apprentissage walk-forward de la limite basse et filtre d'entrée 72 h |
| `results/current_price_learning.csv` | Diagnostics de l'apprentissage : historique, épisodes, volatilité 96 h, multiplicateur et taux de sorties par le bas |
| `src/build_bot_indicators.py` | Indicateurs par bot → `results/bot_indicators.csv` (une ligne par `strategyId`) : volatilité vs pas de grille, ADX, ratio d'efficacité, `trendScore` et régime RANGE/TREND/MIXED (relatif : tiers de l'univers par fréquence, étiquette et jamais un filtre), distance aux bornes en ATR, risque de sortie de fourchette à 96 h, `bestIndicators`. Source : klines Binance si joignables (`dataSource = BINANCE_KLINES`), sinon clôtures horaires de `results/history/` (`PROJECT_HISTORY`, ADX approché, CHOP non publié) |
| `src/analyze_indicator_performance.py` | Walk-forward sur `results/history/` : indicateurs connus à t (klines Binance 1 h si joignables) contre la performance réelle des bots sur les 24 h suivantes (PNL / investissement, trades, sortie de fourchette). Écrit `results/indicator_performance.csv` (IC de Spearman par jour, t-stat, médianes par tiers, colonne `signal`) et `results/regime_performance.csv`. À lire avec `nSlices` : les résultats mûrissent avec l'historique |
| `src/replay_tst_grid.py` | Simulation historique spécifique TSTUSDT (approximation, non-exécution Binance) |
| `tests/` | Tests unitaires et test de bout en bout sur données synthétiques |
| `.github/workflows/audit.yml` | Pipeline horaire (HH:15) + tests et scan complet sur pull request |

## Lecture du CSV final

- `gridProfit` rempli = valeur **exacte** lue sur la page de détail Binance ; vide = estimation, visible uniquement dans `gridProfitEstimateLow/Mid/High`.
- Estimation du Grid Profit (Spot Grid) : calculée aussi quand le prix est **hors plage**. Le prix de départ du bot étant alors inconnu, la fourchette Low/High est plus large (confiance `LOW` dans le brut) et le Mid suppose un départ au centre de la plage.
- `gridProfitTotalProfitRatio` : ratio numérique sans préfixe d'approximation, que le Grid Profit soit exact ou estimé ; vide si le Total Profit est nul. Aucun plafond à 100 % ou 500 %.
- `floatingProfit` : estimation centrale = `totalProfit - gridProfitEstimateMid` quand le Grid Profit est estimé et le Total Profit disponible ; calcul exact = `totalProfit - gridProfit` quand les deux valeurs sont exactes et sur la même base. Une valeur calculée à partir d'une estimation reste une estimation, pas une donnée officielle Binance.
- `totalProfit` : PNL Marketplace (USD) ; remplacé par le Total Profit de la page de détail uniquement pour une cotation stable USD, si cette valeur est cohérente avec le PNL.
- `roi (calculé)` = PNL / investissement minimum × 100 (indicateur de comparaison, pas le ROI Binance).
- `profitPerGridAfterFees` : valeur Binance si exposée ; sinon formule Binance avec frais de 0,1 % par ordre (constante `FEE`). Géométrique : valeur unique. Arithmétique : fourchette. Les frais réels (paire, VIP, BNB) peuvent différer.

Une collecte est exhaustive uniquement si `scan_meta.json` indique `collection_complete: true` (nombre d'identifiants uniques ≥ total annoncé). Cela ne prouve pas que Binance expose toutes les familles de bots.

## Pipeline

`audit_all.py` → `validate.py raw` → `build_multicriteria.py` → `validate.py final` → `build_current_price.py` → `build_grid_setup.py`. Les pages de détail sont visitées dans la limite d'un budget de temps, dans un ordre qui change chaque heure ; les bots non visités restent en estimation, clairement distinguée de l'exact.

Manuel : **Actions → Binance Public Bot Audit → Run workflow**, ou commentaire `/audit-run` sur l'issue d'exploitation. Python 3.11, dépendances dans `requirements.txt` (plus `pandas` et `numpy`).


## Apprentissage de la fourchette Spot Grid

À partir d'au moins 14 jours d'historique, `build_grid_setup.py` estime la volatilité relative des 96 dernières heures et rejoue des départs historiques espacés de 6 heures sur un horizon futur de 96 heures. Pour chaque départ, le prix le plus bas connu jusque-là et la volatilité connue à ce moment servent à calculer la limite basse candidate. Le script choisit le plus petit multiplicateur testé dont le taux historique de cassure basse ne dépasse pas 5 %. Si aucun multiplicateur ne satisfait la cible, le plus grand testé est retenu et `targetMet` reste faux. Les données insuffisantes déclenchent un repli sur la marge fixe de 3 %.

Les diagnostics sont écrits dans `results/current_price_learning.csv`; ce fichier doit rester conservé avec les résultats courants. La limite haute reste le plus haut observé × 1,03. Le filtre d'entrée `WAIT_HIGH_RANGE` continue d'utiliser la position dans le range et le z-score sur 72 heures. L'apprentissage vise à réduire les sorties par le bas ; il ne garantit ni un profit positif ni l'absence de perte, et ses statistiques dépendent du régime de marché historique.

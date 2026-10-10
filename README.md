# Binance Public Bot Audit

Recensement des bots publiquement visibles du Binance Bot Marketplace avec **levier ≤ 1**. Projet en **lecture seule** : aucune clé API, aucun ordre, aucune copie de bot.

## Objectifs

1. **Grid Profit** : rechercher la donnée officielle Binance ; à défaut, estimer au plus fidèle le ratio Grid Profit / Total Profit.
2. **Profit par grille après frais** : le calculer de la manière la plus fiable possible.

Pas de classement : les colonnes `rank` et `score` existent mais restent vides. Aucune ligne n'est retirée pour son âge, son prix hors plage ou son profit par grille.

## Fichiers

| Fichier | Rôle |
|---|---|
| `results/current_multicriteria.csv` | **Livrable** : 29 colonnes figées (ni ajout ni suppression), triées par `strategyId` |
| `results/current_multicriteria_summary.txt` | Résumé court |
| `results/scan_meta.json` | Exhaustivité (collecté vs annoncé par catégorie), pages de détail visitées, validation |
| `results/history/YYYY/MM/DD/*.csv.gz` | Brut horaire avec provenance, 30 jours |
| `src/audit_all.py` | Collecte + Grid Profit exact/estimé + profit par grille → CSV brut |
| `src/validate.py` | Contrôles `raw` (provenance) puis `final` (schéma et calculs) |
| `src/build_multicriteria.py` | Réécrit le CSV brut en 29 colonnes |
| `tests/` | Tests unitaires et test de bout en bout sur données synthétiques |
| `.github/workflows/audit.yml` | Pipeline horaire (HH:15) + tests et scan complet sur pull request |

## Lecture du CSV final

- `gridProfit` rempli = valeur **exacte** lue sur la page de détail Binance ; vide = estimation, visible uniquement dans `gridProfitEstimateLow/Mid/High`.
- Estimation du Grid Profit (Spot Grid) : calculée aussi quand le prix est **hors plage**. Le prix de départ du bot étant alors inconnu, la fourchette Low/High est plus large (confiance `LOW` dans le brut) et le Mid suppose un départ au centre de la plage.
- `gridProfitTotalProfitRatio` : numérique si le Grid Profit est exact ; préfixé `≈` s'il est estimé ; vide si le Total Profit est nul. Aucun plafond à 100 % ou 500 %.
- `floatingProfit` : vide dès que le Grid Profit n'est pas exact.
- `totalProfit` : PNL Marketplace (USD) ; remplacé par le Total Profit de la page de détail uniquement pour une cotation stable USD, si cette valeur est cohérente avec le PNL.
- `roi (calculé)` = PNL / investissement minimum × 100 (indicateur de comparaison, pas le ROI Binance).
- `profitPerGridAfterFees` : valeur Binance si exposée ; sinon formule Binance avec frais de 0,1 % par ordre (constante `FEE`). Géométrique : valeur unique. Arithmétique : fourchette. Les frais réels (paire, VIP, BNB) peuvent différer.

Une collecte est exhaustive uniquement si `scan_meta.json` indique `collection_complete: true` (nombre d'identifiants uniques ≥ total annoncé). Cela ne prouve pas que Binance expose toutes les familles de bots.

## Pipeline

`audit_all.py` → `validate.py raw` → `build_multicriteria.py` → `validate.py final`. Les pages de détail sont visitées dans la limite d'un budget de temps, dans un ordre qui change chaque heure ; les bots non visités restent en estimation, clairement distinguée de l'exact.

Manuel : **Actions → Binance Public Bot Audit → Run workflow**, ou commentaire `/audit-run` sur l'issue d'exploitation. Python 3.11, dépendances dans `requirements.txt` (plus `pandas` et `numpy`).

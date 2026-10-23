# current_price.csv — réglage de grille et filtre d'entrée

`src/build_grid_setup.py` ajoute 7 colonnes à `results/current_price.csv` à partir de `results/history/` (prix horaire par paire). Ce sont des aides
à la décision, ni une prévision ni un conseil d'investissement.

Dans le CSV, `priceLow` et `priceHigh` sont les extrêmes observés dans les instantanés du projet depuis le début de son historique ;
`binancePriceLow` et `binancePriceHigh` sont les extrêmes Binance depuis la cotation, vérifiés par comparaison des bougies mensuelles et de l'historique journalier. Ces colonnes ne sont pas les bornes d'une grille recommandée.

| Colonne | Sens |
|---|---|
| `recommendedPriceLow` | Si l'apprentissage dispose d'au moins 14 jours et d'épisodes suffisants : plus bas observé × max(0,01 ; 1 − multiplicateur retenu × volatilité relative sur 96 h). Sinon : plus bas observé × 0,97 |
| `recommendedPriceHigh` | Plus haut de tout l'historique de prix disponible x 1,03 |
| `recommendedGridCount` / `recommendedGridMode` | 5 grilles, `GEOMETRIC` |
| `rangePosition72hPct` | Position du prix dans le range des 72 h (0 = au plus bas, 100 = au plus haut) |
| `zScore72h` | Écart du prix à sa moyenne des 72 h, en écarts-types (positif = prix haut) |
| `setupSignal` | `WAIT_HIGH_RANGE` si position > 80 ou z-score > 0,3 ; sinon `READY_TO_REVIEW`. `INSUFFICIENT_HISTORY`, `INSUFFICIENT_RANGE`, `INSUFFICIENT_72H`, `NO_DATA` si les données manquent |

## Pourquoi ces règles (backtest walk-forward, 11-22 octobre 2026)

Pour 111 paires et un départ toutes les 6 h : réglage calculé sur les 96 h précédentes, puis grille simulée sur les 96 h suivantes
(ordres par cellule, 0,1 % de frais, 1 210 évaluations par réglage).

| Réglage | Moyenne 4 j | Gagnants | 10 % pires | 1 % pires | Prix hors fourchette |
|---|---|---|---|---|---|
| Bas = plus bas, haut = plus haut, 5 grilles (sans marge) | +0,69 % | 67 % | −2,63 % | −6,81 % | 8,2 % |
| **Avec marge de 3 % (version actuelle)** | **+0,76 %** | 66 % | −2,42 % | −5,88 % | 2,6 % |
| ±10 % autour du prix, 10 grilles | +0,31 % | 62 % | −2,47 % | −6,90 % | 2,8 % |

- Moins de grilles fait mieux : 5 grilles +0,71 %, 6 : +0,63 %, 8 : +0,52 %, 10 : +0,44 %.
- Signal : les départs `WAIT_HIGH_RANGE` (23 %) rapportent −0,25 % en moyenne (50 % de gagnants), les autres +1,07 % (70 % de gagnants).

## Le signal sur TST, le 11 octobre 2026 à 13:45 (départ du bot simulé)

L'historique du dépôt contient des prix depuis le 7 octobre. Sur les 72 h avant le départ (74 points) : prix 0,01766, moyenne 0,01754,
plus haut 0,01817 (atteint 4 h plus tôt, à 9:29). Le z-score est de **0,47** (> 0,3) : le signal aurait été `WAIT_HIGH_RANGE`.
La position dans le range (57,5 %) n'aurait pas suffi à elle seule : seul le z-score dépasse son seuil, et de peu.

## Exemple : TSTUSDT (état du dépôt le 22/10/2026 vers 18 h UTC)

| Colonne | Valeur | Lecture |
|---|---|---|
| `currentPrice` | 0,01511 | prix actuel |
| `rangePosition72hPct` | 0,0 | le prix est à son plus bas des 72 h |
| `zScore72h` | −1,54 | très sous sa moyenne récente : pas un prix haut |
| `setupSignal` | `READY_TO_REVIEW` | aucune raison d'attendre au sens du filtre ; il ne détecte pas une baisse en cours |
| `recommendedPriceLow` | 0,014133 | 0,01457 (plus bas de l'historique) x 0,97 |
| `recommendedPriceHigh` | 0,018715 | 0,01817 (plus haut de l'historique, le 11 oct. à 9:29) x 1,03 |
| `recommendedGridCount` / mode | 5 / `GEOMETRIC` | lignes : 0,014133 · 0,014949 · 0,015813 · 0,016727 · 0,017693 · 0,018715 |

Lecture avec 100 USDT (5 grilles de 20 USDT) :
- le pas géométrique est de 5,78 %, soit environ 5,57 % net par cycle après frais, donc ≈ 1,1 USDT par cycle complet (20 x 5,57 %) ;
- le prix est à 6,9 % du bas et à 23,9 % du haut ; sous 0,014133 le bot détient tout son capital en TST et ne trade plus ;
- trois cellules sur cinq sont au-dessus du prix : le bot détient du TST dès le départ (environ 60 USDT), donc il subit une baisse qui continue.

Contre-exemple : OGUSDT (prix 2,656) a `rangePosition72hPct` = 100 et `zScore72h` = 3,60, donc `WAIT_HIGH_RANGE` : le prix est au plus haut de son range,
le cas qui a fait perdre le bot TST simulé.

## Limites

- Historique court (11-22 octobre), un seul régime de marché ; seuils (0,3 ; 80 ; 3 % ; 5 grilles) à revalider quand l'historique s'allonge.
- Le tableau ci-dessus compare des réglages de référence avec une borne basse à marge fixe ; il ne constitue pas un backtest indépendant de la stratégie adaptative actuelle. L'apprentissage est walk-forward pour choisir le multiplicateur, mais la performance de la stratégie adaptative doit encore être évaluée sur une période de test séparée.
- Le backtest utilise une fenêtre de 96 h pour le plus bas / plus haut ; la colonne utilise tout l'historique disponible (`HISTORY_DAYS = None`), comme demandé. Cette version n'a pas pu être testée en walk-forward faute de recul.
  Pour TST, 96 h donne 0,014133 à 0,01681, tout l'historique donne 0,014133 à 0,018715 : le haut vient du pic du 11 octobre, loin du prix actuel.
- Les prix horaires du dépôt commencent le 7 octobre 2026 et sont conservés 30 jours : l'historique utilisé s'arrête donc de s'allonger vers le 6 novembre.
- Frais de 0,1 % sans remise BNB, pas de glissement, prix horaires. Arrondir les prix au pas accepté par Binance.

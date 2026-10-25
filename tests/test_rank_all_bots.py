import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))
import rank_all_bots as rk  # noqa: E402


def bots():
    base = dict(gridCount=10, currentPrice=100.0, priceRangeLow=80.0, priceRangeHigh=125.0, gridMode="ARITHMETIC",
                profitPerGridAfterFeesPct=1.0, volOverStep=2.0, exitRisk96hPct=10.0, trendScore=30.0, hurst=0.45)
    rows = [
        dict(base, strategyId=1),                                            # bot courant, marché plutôt range
        dict(base, strategyId=2, volOverStep=4.0),                           # même marché, plus actif
        dict(base, strategyId=3, currentPrice=130.0),                        # hors plage
        dict(base, strategyId=4, profitPerGridAfterFeesPct=-0.05),           # pas inférieur aux frais
        dict(base, strategyId=5, volOverStep=np.nan, exitRisk96hPct=np.nan, trendScore=np.nan),  # sans données
        dict(base, strategyId=6, trendScore=90.0, hurst=0.62),               # marché en forte tendance
    ]
    return pd.DataFrame(rows)


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.out = rk.score(bots()).set_index("strategyId")

    def test_no_row_is_dropped_and_ranks_are_permutations(self):
        self.assertEqual(len(self.out), 6)
        for col in ("rankGridFavorability", "rankProfitability", "rankProbability"):
            self.assertEqual(sorted(self.out[col]), [1, 2, 3, 4, 5, 6])

    def test_profitability_and_probability_logic(self):
        o = self.out
        self.assertLess(o.loc[2, "rankProfitability"], o.loc[1, "rankProfitability"])   # plus actif : plus rentable
        self.assertEqual(o.loc[3, "expectedGridReturnPctPerDay"], 0.0)                  # hors plage : pas de cycles
        self.assertEqual(o.loc[3, "pSuccess96hPct"], 0.0)
        self.assertEqual(o.loc[4, "pCycle96hPct"], 0.0)                                 # aucun cycle rentable
        self.assertEqual(o.loc[5, "probabilityTier"], "")

    def test_favorability_components(self):
        o = self.out
        self.assertGreater(o.loc[1, "regimeScore"], o.loc[6, "regimeScore"])      # range > tendance
        self.assertEqual(o.loc[3, "centralityScore"], 0.0)                         # hors plage
        self.assertEqual(o.loc[4, "economicsScore"], 0.0)                          # profit par grille <= 0
        self.assertEqual(o.loc[1, "economicsScore"], 100.0)                        # >= 0,5 %
        self.assertAlmostEqual(rk.W_REGIME + rk.W_CENTER + rk.W_ECON, 1.0)

    def test_centrality_uses_log_scale_for_geometric_bots(self):
        g = bots().iloc[[0]].copy()
        g["gridMode"] = "GEOMETRIC"
        g["priceRangeLow"], g["priceRangeHigh"], g["currentPrice"] = 100.0, 400.0, 200.0   # milieu géométrique
        self.assertAlmostEqual(rk.score(g)["centralityScore"].iloc[0], 100.0)

    def test_favorability_comes_first_then_profitability_then_probability(self):
        o = self.out
        self.assertEqual(o.loc[5, "rankGridFavorability"], 6)                      # sans donnée : dernier, conservé
        self.assertEqual(o.loc[5, "gridFavorabilityLabel"], "")
        self.assertLess(o.loc[1, "rankGridFavorability"], o.loc[6, "rankGridFavorability"])   # range avant tendance
        # même niveau : la probabilité passe avant la rentabilité
        a = bots().iloc[[0, 0]].reset_index(drop=True)
        a["strategyId"] = [10, 11]
        a.loc[0, ["volOverStep", "exitRisk96hPct"]] = [1.0, 0.0]    # très sûr mais peu rentable
        a.loc[1, ["volOverStep", "exitRisk96hPct"]] = [3.0, 60.0]   # rentable mais risqué
        r = rk.score(a).set_index("strategyId")
        self.assertEqual(r.loc[10, "gridFavorabilityLabel"], r.loc[11, "gridFavorabilityLabel"])
        self.assertGreater(r.loc[11, "expectedGridReturnPctPerDay"], r.loc[10, "expectedGridReturnPctPerDay"])
        self.assertGreater(r.loc[10, "pSuccess96hPct"], r.loc[11, "pSuccess96hPct"])
        self.assertLess(r.loc[10, "rankGridFavorability"], r.loc[11, "rankGridFavorability"])
        tiers = o.sort_values("rankGridFavorability")["gridFavorabilityLabel"].tolist()
        order = [name for _, name in rk.LABELS]
        idx = [order.index(t) for t in tiers if t]
        self.assertEqual(idx, sorted(idx))                                          # niveaux dans l'ordre

    def test_quintile_only_for_scored_bots(self):
        q = self.out["profitabilityQuintile"]
        self.assertTrue(np.isnan(q[5]))
        self.assertEqual(q[2], 1.0)


if __name__ == "__main__":
    unittest.main()

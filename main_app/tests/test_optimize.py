from datetime import UTC, date, datetime

from django.test import SimpleTestCase

from main_app.services.optimize import chain_oos, enumerate_combos, grid_from_schema, rank, stability, walk_forward_windows


class WalkForwardWindowsRollWithoutLeaking(SimpleTestCase):
    def test_windows_are_contiguous_and_non_overlapping(self):
        w = walk_forward_windows(date(2026, 6, 1), date(2026, 8, 31), 30, 10)
        self.assertEqual(len(w), 6)
        for x in w:
            self.assertEqual((x['test_start'] - x['train_end']).days, 1)
            self.assertEqual((x['train_end'] - x['train_start']).days, 29)
        for a, b in zip(w, w[1:]):
            self.assertEqual((b['train_start'] - a['train_start']).days, 10)
        self.assertLessEqual(w[-1]['test_end'], date(2026, 8, 31))

    def test_too_short_range_gives_no_windows(self):
        self.assertEqual(walk_forward_windows(date(2026, 8, 1), date(2026, 8, 20), 30, 10), [])


class GridsAreBoundedAndOverridable(SimpleTestCase):
    def test_grid_from_schema_uses_ranges_and_keeps_bools_fixed(self):
        g = grid_from_schema('orb')
        self.assertEqual(g['trade_short'], [False])
        self.assertEqual(g['range_minutes'], [5, 15, 30])
        self.assertTrue(all(0.5 <= v <= 2.0 for v in g['stop_atr_mult']))
        g2 = grid_from_schema('orb', {'rr': [1.5, 3], 'trade_short': [True]})
        self.assertEqual(g2['rr'], [1.5, 3.0])
        self.assertEqual(g2['trade_short'], [True])

    def test_combo_caps(self):
        g = {'a': [1, 2, 3, 4, 5], 'b': [1, 2, 3, 4, 5], 'c': [1, 2, 3]}
        self.assertEqual(len(enumerate_combos(g, 'grid', 20)), 20)
        r = enumerate_combos(g, 'random', 12, seed=1)
        self.assertEqual(len(r), 12)
        self.assertEqual(len({tuple(sorted(c.items())) for c in r}), 12)
        self.assertEqual(len(enumerate_combos({'a': [1, 2]}, 'random', 50)), 2)


class RankingAndStability(SimpleTestCase):
    def _res(self, params, sharpe, trades=20):
        m = {'trades': trades, 'net_pnl': sharpe * 10, 'sharpe': sharpe, 'profit_factor': 1.0, 'win_rate': 50.0,
             'max_drawdown_pct': -1.0, 'expectancy': 1.0}
        return (params, m, [], [], 0, 0)

    def test_rank_respects_min_trades(self):
        ranked = rank([self._res({'a': 1}, 2.0, trades=5), self._res({'a': 2}, 1.0)], 'sharpe', 10)
        self.assertEqual(ranked[0]['params'], {'a': 2})
        self.assertEqual(ranked[1]['objective'], float('-inf'))

    def test_stability_scores_a_plateau_higher_than_a_spike(self):
        grid = {'a': [1, 2, 3]}
        plateau = rank([self._res({'a': 1}, 1.8), self._res({'a': 2}, 2.0), self._res({'a': 3}, 1.9)], 'sharpe', 0)
        spike = rank([self._res({'a': 1}, 0.1), self._res({'a': 2}, 2.0), self._res({'a': 3}, 0.0)], 'sharpe', 0)
        self.assertGreater(stability(plateau, grid)['score'], stability(spike, grid)['score'])

    def test_chain_oos_offsets_equity(self):
        t = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
        seg1 = ([], [(t, 10000, 0, 10000, 0), (t, 10100, 0, 10100, 100)], 10, 2)
        seg2 = ([], [(t, 10000, 0, 10000, 0), (t, 9950, 0, 9950, -50)], 10, 3)
        trades, eq, bs, bp = chain_oos([seg1, seg2], 10000)
        self.assertEqual([e[3] for e in eq], [10000, 10100, 10100, 10050])
        self.assertEqual((bs, bp), (20, 5))

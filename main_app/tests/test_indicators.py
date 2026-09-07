import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from main_app.services import indicators as ind
from main_app.services.strategies.base import volume_evidence
from main_app.tests.helpers import frames_for


class IndicatorsAreCausalAndBounded(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.df = frames_for(['QQQ'])['QQQ']
        cls.session = ind.session_key(cls.df.index, 'stock')

    def test_rsi_stays_within_0_100(self):
        r = ind.rsi(self.df['close'], 14)
        self.assertTrue(((r >= 0) & (r <= 100)).all())

    def test_opening_range_is_nan_until_complete_then_matches_first_bars(self):
        hi, lo = ind.opening_range(self.df, self.session, 3)
        pos = ind.bar_position(self.session)
        self.assertTrue(hi[pos < 3].isna().all())
        first_day = self.df[self.session == self.session.iloc[0]]
        self.assertAlmostEqual(hi[pos == 3].iloc[0], first_day['high'].iloc[:3].max())
        self.assertAlmostEqual(lo[pos == 3].iloc[0], first_day['low'].iloc[:3].min())

    def test_values_do_not_depend_on_future_bars(self):
        full = ind.ema(self.df['close'], 9)
        cut = ind.ema(self.df['close'].iloc[:100], 9)
        self.assertTrue(np.allclose(full.iloc[:100].to_numpy(), cut.to_numpy()))
        full_v = ind.session_vwap(self.df, self.session)
        cut_v = ind.session_vwap(self.df.iloc[:100], self.session.iloc[:100])
        self.assertTrue(np.allclose(full_v.iloc[:100].to_numpy(), cut_v.to_numpy()))

    def test_relative_volume_first_session_is_explicitly_unavailable(self):
        rv = ind.relative_volume(self.df, self.session, 10)
        first = rv[self.session == self.session.iloc[0]]
        self.assertTrue(first.isna().all())
        self.assertGreater(rv.iloc[-1], 0)

    def test_zero_volume_bars_are_not_invented_as_average_volume(self):
        df = self.df.copy()
        df.loc[df.index[-3:], 'volume'] = 0.0
        rv = ind.relative_volume(df, self.session, 10)
        self.assertTrue(rv.iloc[-3:].isna().all())

    def test_volume_filter_blocks_unknown_data_but_documents_forex_fallback(self):
        ok, text, value = volume_evidence('crypto', np.nan, 1.5)
        self.assertFalse(ok)
        self.assertIsNone(value)
        self.assertIn('unavailable', text)
        ok, text, value = volume_evidence('forex', np.nan, 1.0)
        self.assertTrue(ok)
        self.assertIsNone(value)
        self.assertIn('price-only fallback', text)

    def test_session_anchor_discloses_when_volume_is_unavailable(self):
        df = self.df.copy()
        df['volume'] = 0.0
        vwap = ind.session_vwap(df, self.session)
        typical = (df['high'] + df['low'] + df['close']) / 3
        expected = typical.groupby(self.session).expanding().mean().reset_index(level=0, drop=True)
        self.assertTrue(np.allclose(vwap, expected))
        self.assertFalse(ind.session_has_volume(df, self.session).any())

    def test_minutes_to_close_counts_down(self):
        mtc = ind.minutes_to_close(self.df.index, 'stock')
        day = mtc[self.session == self.session.iloc[0]]
        self.assertEqual(day.iloc[0], 390)
        self.assertEqual(day.iloc[-1], 5)
        self.assertTrue(ind.minutes_to_close(self.df.index[:3], 'crypto').isna().all())

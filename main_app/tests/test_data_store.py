"""The quality gate: what a bar has to be before a strategy is allowed to see it.

This had no test coverage at all, which is how a bar at 1,248x the true price sat
in the PEPE/USD history for nine months. Every stop and target on this desk is
sized in ATRs, and ATR is computed from these bars, so a single impossible price
is not a cosmetic problem — it is fourteen bars of wrong risk.
"""
import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from main_app.services.data.store import OUTLIER_FACTOR, quality_gate


def frame(closes, freq='15min', volume=1000.0):
    a = np.asarray(closes, dtype=float)
    idx = pd.date_range('2026-01-01', periods=len(a), freq=freq, tz='UTC')
    return pd.DataFrame({'open': a, 'high': a * 1.01, 'low': a * 0.99,
                         'close': a, 'volume': volume}, index=idx)


class TheGateRejectsBarsThatContradictThemselves(SimpleTestCase):
    def test_a_high_below_the_close_is_dropped(self):
        df = frame(np.full(50, 100.0))
        df.iloc[10, df.columns.get_loc('high')] = 50.0
        out, rep = quality_gate(df, '15Min', 'crypto')
        self.assertEqual(rep.dropped_bad_ohlc, 1)
        self.assertEqual(len(out), 49)

    def test_a_low_above_the_open_is_dropped(self):
        df = frame(np.full(50, 100.0))
        df.iloc[10, df.columns.get_loc('low')] = 150.0
        out, rep = quality_gate(df, '15Min', 'crypto')
        self.assertEqual(rep.dropped_bad_ohlc, 1)

    def test_a_non_positive_price_is_dropped(self):
        df = frame(np.full(50, 100.0))
        df.iloc[10, df.columns.get_loc('low')] = 0.0
        out, rep = quality_gate(df, '15Min', 'crypto')
        self.assertEqual(rep.dropped_bad_ohlc, 1)

    def test_duplicate_timestamps_keep_the_last(self):
        df = frame(np.full(50, 100.0))
        df = pd.concat([df, df.iloc[[10]]]).sort_index()
        out, rep = quality_gate(df, '15Min', 'crypto')
        self.assertEqual(rep.dropped_dupes, 1)
        self.assertEqual(len(out), 50)

    def test_a_clean_frame_passes_untouched(self):
        df = frame(np.full(50, 100.0))
        out, rep = quality_gate(df, '15Min', 'crypto')
        self.assertEqual(len(out), 50)
        self.assertEqual(rep.issues, [])


class ImpossiblePricesAreDropped(SimpleTestCase):
    """A bar can agree with itself and still be nonsense.

    The checks above only ask whether a bar is internally consistent. The real
    PEPE/USD bar had a high above its open and close, a low below them, and every
    price positive — and was wrong by a factor of 1,248, because the feed returned
    the right number in the wrong units.
    """

    def test_a_thousandfold_glitch_is_removed(self):
        closes = np.full(300, 3.9e-06)
        closes[150] = 0.0049                       # the bar that was actually there
        out, rep = quality_gate(frame(closes), '15Min', 'crypto')
        self.assertEqual(rep.dropped_outliers, 1)
        self.assertEqual(len(out), 299)
        self.assertTrue(any('impossible' in i for i in rep.issues))

    def test_a_real_thirty_seven_percent_gap_survives(self):
        """AMD genuinely moved 37.5% on 2025-10-06. Dropping that would be a
        worse bug than keeping the glitch."""
        closes = np.concatenate([np.full(150, 164.0), np.full(150, 226.0)])
        out, rep = quality_gate(frame(closes, '5min'), '5Min', 'stock')
        self.assertEqual(rep.dropped_outliers, 0)
        self.assertEqual(len(out), 300)

    def test_a_coin_that_genuinely_quintuples_survives(self):
        out, rep = quality_gate(frame(np.linspace(1.0, 5.0, 300)), '15Min', 'crypto')
        self.assertEqual(rep.dropped_outliers, 0)

    def test_the_threshold_is_far_above_any_real_move(self):
        """Guard the constant itself: anything under about 5x would start
        deleting real crypto weeks."""
        self.assertGreaterEqual(OUTLIER_FACTOR, 8.0)

    def test_a_short_frame_is_left_alone(self):
        """With too little history there is no reference to judge against, and
        guessing is worse than passing it through."""
        out, rep = quality_gate(frame(np.full(15, 3.9e-06)), '15Min', 'crypto')
        self.assertEqual(rep.dropped_outliers, 0)
        self.assertEqual(len(out), 15)

    def test_a_glitch_at_the_very_start_is_still_caught(self):
        """The rolling median is centred, so an outlier in the first bars has a
        reference to the right of it even with nothing to the left."""
        closes = np.full(300, 3.9e-06)
        closes[2] = 0.0049
        _, rep = quality_gate(frame(closes), '15Min', 'crypto')
        self.assertEqual(rep.dropped_outliers, 1)

    def test_a_corrupted_high_alone_is_caught(self):
        """The real bar had a correct open and low and a corrupted high and close.
        ATR is computed from the high and the low, so a bad high is wrong risk
        even when the close looks perfectly reasonable."""
        df = frame(np.full(300, 3.9e-06))
        df.iloc[150, df.columns.get_loc('high')] = 0.0049
        _, rep = quality_gate(df, '15Min', 'crypto')
        self.assertEqual(rep.dropped_outliers, 1)

    def test_a_corrupted_low_alone_is_caught(self):
        df = frame(np.full(300, 100.0))
        df.iloc[150, df.columns.get_loc('low')] = 0.0001
        _, rep = quality_gate(df, '15Min', 'crypto')
        self.assertEqual(rep.dropped_outliers, 1)

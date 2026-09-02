from datetime import UTC, date, datetime

from django.test import SimpleTestCase

from main_app.services.data import calendar as cal


class CalendarKnowsHolidaysAndEarlyCloses(SimpleTestCase):
    def test_labor_day_2026_is_closed(self):
        self.assertIsNone(cal.session_for(date(2026, 9, 7)))
        self.assertFalse(cal.is_trading_day(date(2026, 9, 7)))

    def test_regular_session_bounds_in_utc(self):
        s = cal.session_for(date(2026, 9, 1))  # EDT: 09:30 ET = 13:30 UTC
        self.assertEqual(s.open_utc, datetime(2026, 9, 1, 13, 30, tzinfo=UTC))
        self.assertEqual(s.close_utc, datetime(2026, 9, 1, 20, 0, tzinfo=UTC))
        self.assertEqual(s.minutes, 390)

    def test_black_friday_closes_at_one(self):
        s = cal.session_for(date(2026, 11, 27))  # EST after Nov 1
        self.assertTrue(s.early_close)
        self.assertEqual(s.close_utc, datetime(2026, 11, 27, 18, 0, tzinfo=UTC))

    def test_dst_edges(self):
        self.assertEqual(cal.session_for(date(2026, 3, 6)).open_utc.hour, 14)   # EST before Mar 8
        self.assertEqual(cal.session_for(date(2026, 3, 9)).open_utc.hour, 13)   # EDT after
        self.assertEqual(cal.session_for(date(2026, 10, 30)).open_utc.hour, 13)
        self.assertEqual(cal.session_for(date(2026, 11, 2)).open_utc.hour, 14)

    def test_next_open_from_a_weekend(self):
        sat = datetime(2026, 9, 5, 15, 0, tzinfo=UTC)
        self.assertEqual(cal.next_open(sat), datetime(2026, 9, 8, 13, 30, tzinfo=UTC))  # Tue after Labor Day

    def test_session_at_and_is_open(self):
        inside = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
        outside = datetime(2026, 9, 1, 21, 0, tzinfo=UTC)
        self.assertIsNotNone(cal.session_at(inside))
        self.assertIsNone(cal.session_at(outside))
        self.assertTrue(cal.is_open(outside, 'crypto'))
        self.assertFalse(cal.is_open(outside, 'stock'))

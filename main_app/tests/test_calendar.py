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


class ASessionKnowsWhenItOpened(SimpleTestCase):
    def test_a_stock_session_opened_at_the_bell(self):
        inside = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
        self.assertEqual(cal.session_open(inside), datetime(2026, 9, 1, 13, 30, tzinfo=UTC))

    def test_the_forex_week_opened_on_sunday_at_five_eastern(self):
        opened = datetime(2026, 10, 11, 21, 0, tzinfo=UTC)          # Sunday 17:00 EDT
        for ts in (opened, datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
                   datetime(2026, 10, 16, 20, 59, tzinfo=UTC)):       # to Friday, a minute before the close
            self.assertEqual(cal.forex_week_open(ts), opened)
            self.assertEqual(cal.session_open(ts, 'forex'), opened)
        self.assertEqual(cal.forex_week_open(datetime(2026, 1, 14, 12, 0, tzinfo=UTC)),
                         datetime(2026, 1, 11, 22, 0, tzinfo=UTC))    # Sunday 17:00 EST

    def test_a_closed_market_has_no_open_and_crypto_never_had_one(self):
        saturday = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)
        self.assertIsNone(cal.session_open(saturday))
        self.assertIsNone(cal.session_open(saturday, 'forex'))
        self.assertIsNone(cal.forex_week_open(datetime(2026, 10, 11, 20, 59, tzinfo=UTC)))
        self.assertIsNone(cal.session_open(saturday, 'crypto'))
        self.assertIsNone(cal.session_open(datetime(2026, 10, 14, 15, 0, tzinfo=UTC), 'crypto'))


class TheLastCompletedSessionIsKnownWhetherOrNotTheMarketIsOpen(SimpleTestCase):
    def test_stocks_mid_session_answer_yesterdays_bell_and_after_it_todays(self):
        monday_bell = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)
        self.assertEqual(cal.last_close(datetime(2026, 10, 13, 15, 0, tzinfo=UTC)), monday_bell)
        self.assertEqual(cal.last_close(datetime(2026, 10, 12, 20, 0, tzinfo=UTC)), monday_bell)
        self.assertEqual(cal.last_close(datetime(2026, 10, 13, 9, 0, tzinfo=UTC)), monday_bell)   # 02:00 PT

    def test_stocks_skip_the_weekend_and_a_holiday(self):
        friday_bell = datetime(2026, 9, 4, 20, 0, tzinfo=UTC)
        self.assertEqual(cal.last_close(datetime(2026, 9, 6, 15, 0, tzinfo=UTC)), friday_bell)
        self.assertEqual(cal.last_close(datetime(2026, 9, 7, 18, 0, tzinfo=UTC)), friday_bell)    # Labor Day
        self.assertEqual(cal.last_close(datetime(2026, 9, 8, 14, 0, tzinfo=UTC)), friday_bell)    # Tuesday, open

    def test_stocks_use_the_early_close(self):
        self.assertEqual(cal.last_close(datetime(2026, 11, 27, 19, 0, tzinfo=UTC)),
                         datetime(2026, 11, 27, 18, 0, tzinfo=UTC))

    def test_forex_is_the_last_weekday_roll(self):
        friday_roll = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)
        for ts in (datetime(2026, 10, 10, 15, 0, tzinfo=UTC),         # Saturday
                   datetime(2026, 10, 11, 22, 0, tzinfo=UTC),         # Sunday, the new week is open
                   datetime(2026, 10, 12, 20, 59, tzinfo=UTC)):       # Monday, a minute before the roll
            self.assertEqual(cal.last_close(ts, 'forex'), friday_roll)
        self.assertEqual(cal.last_close(datetime(2026, 10, 12, 21, 0, tzinfo=UTC), 'forex'),
                         datetime(2026, 10, 12, 21, 0, tzinfo=UTC))

    def test_crypto_has_none(self):
        self.assertIsNone(cal.last_close(datetime(2026, 10, 12, 15, 0, tzinfo=UTC), 'crypto'))


class TheYahooForexFeedResumesLaterThanTheMarketOpens(SimpleTestCase):
    """Monday 00:00 London, not Sunday 17:00 New York."""

    def test_two_hours_after_the_open_in_summer_and_in_winter(self):
        self.assertEqual(cal.feed_resumes_at(datetime(2026, 10, 11, 21, 10, tzinfo=UTC), 'forex'),
                         datetime(2026, 10, 11, 23, 0, tzinfo=UTC))   # BST: 00:00 London = 23:00 UTC
        self.assertEqual(cal.feed_resumes_at(datetime(2026, 1, 11, 22, 10, tzinfo=UTC), 'forex'),
                         datetime(2026, 1, 12, 0, 0, tzinfo=UTC))     # GMT

    def test_three_hours_in_a_week_the_clocks_disagree(self):
        """London left summer time on 2026-10-25; New York not until 11-01."""
        self.assertEqual(cal.forex_week_open(datetime(2026, 10, 25, 22, 0, tzinfo=UTC)),
                         datetime(2026, 10, 25, 21, 0, tzinfo=UTC))
        self.assertEqual(cal.feed_resumes_at(datetime(2026, 10, 25, 22, 0, tzinfo=UTC), 'forex'),
                         datetime(2026, 10, 26, 0, 0, tzinfo=UTC))

    def test_it_is_the_same_moment_all_week(self):
        self.assertEqual(cal.feed_resumes_at(datetime(2026, 10, 15, 9, 0, tzinfo=UTC), 'forex'),
                         datetime(2026, 10, 11, 23, 0, tzinfo=UTC))

    def test_stocks_resume_at_the_bell_and_a_closed_or_endless_market_has_no_answer(self):
        self.assertEqual(cal.feed_resumes_at(datetime(2026, 9, 1, 15, 0, tzinfo=UTC)),
                         datetime(2026, 9, 1, 13, 30, tzinfo=UTC))
        self.assertIsNone(cal.feed_resumes_at(datetime(2026, 9, 1, 21, 0, tzinfo=UTC)))
        self.assertIsNone(cal.feed_resumes_at(datetime(2026, 10, 10, 15, 0, tzinfo=UTC), 'forex'))
        self.assertIsNone(cal.feed_resumes_at(datetime(2026, 9, 1, 15, 0, tzinfo=UTC), 'crypto'))

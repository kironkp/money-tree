"""The running commentary.

Everything the agent notices, decides or does becomes one short line the
dashboard streams. Lines are buffered and written in one bulk insert per
tick (SQLite likes that), and mirrored to the process log so a terminal
shows the same story.
"""
from __future__ import annotations

import logging
from datetime import datetime

from django.utils import timezone

from main_app.models import FeedEvent

log = logging.getLogger('moneytree.feed')


class Narrator:
    def __init__(self, account=None, echo: bool = True):
        self.account = account
        self.echo = echo
        self.buffer: list[FeedEvent] = []

    def say(self, level: str, text: str, symbol: str = '', strategy_key: str = '', data: dict | None = None,
            ts: datetime | None = None) -> None:
        text = text[:400]
        self.buffer.append(FeedEvent(account=self.account, ts=ts or timezone.now(), level=level, symbol=symbol,
                                     strategy_key=strategy_key, text=text, data=data or {}))
        if self.echo:
            log.info('%-6s %s', level, text)
        if len(self.buffer) >= 200:
            self.flush()

    def flush(self) -> int:
        if not self.buffer:
            return 0
        rows, self.buffer = self.buffer, []
        try:
            FeedEvent.objects.bulk_create(rows, batch_size=500)
        except Exception:  # never let commentary kill the loop
            log.exception('feed flush failed')
        return len(rows)


class NullNarrator:
    """Backtests are silent."""

    def say(self, *args, **kwargs) -> None:
        pass

    def flush(self) -> int:
        return 0

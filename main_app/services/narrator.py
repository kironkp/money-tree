"""The running commentary.

Everything the agent notices, decides or does becomes one line the dashboard
streams. `ts` is always the wall clock (when it happened); `bar_ts` is the
market bar it is about. Lines are buffered and written in one bulk insert per
tick, and mirrored to the process log.
"""
from __future__ import annotations

import logging
from datetime import datetime

from django.utils import timezone

from main_app.models import FeedEvent, TradeCard

log = logging.getLogger('moneytree.feed')


class Narrator:
    def __init__(self, account=None, echo: bool = True):
        self.account = account
        self.echo = echo
        self.buffer: list[FeedEvent] = []

    def say(self, level: str, text: str, symbol: str = '', strategy_key: str = '', data: dict | None = None,
            ts: datetime | None = None, bar_ts: datetime | None = None, phase: str = '', card_id: str = '') -> None:
        text = text[:600]
        data = dict(data or {})
        if card_id:
            data['card'] = card_id
        phase = phase or {'bar': 'evaluate', 'signal': 'decide', 'order': 'submit', 'fill': 'fill', 'trade': 'close',
                          'risk': 'alert', 'error': 'alert', 'journal': 'system'}.get(level, 'system')
        self.buffer.append(FeedEvent(account=self.account, ts=timezone.now(), bar_ts=bar_ts or ts, level=level,
                                     phase=phase, symbol=symbol, strategy_key=strategy_key, text=text, data=data))
        if self.echo:
            log.info('%-6s %s', level, text)
        if len(self.buffer) >= 200:
            self.flush()

    def flush(self) -> int:
        if not self.buffer:
            return 0
        rows, self.buffer = self.buffer, []
        try:
            ids = {r.data.get('card') for r in rows if r.data.get('card')}
            if ids:
                cards = {c.entry_order_id: c for c in TradeCard.objects.filter(entry_order_id__in=ids)}
                for r in rows:
                    cid = r.data.get('card')
                    if cid in cards:
                        r.card = cards[cid]
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

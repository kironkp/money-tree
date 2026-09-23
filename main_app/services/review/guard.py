"""The reviewer may look at everything and change almost nothing.

The rule the owner set is that neither review cycle may alter strategy
configuration, raise a risk limit, or arm live trading. A comment saying so is
not a control, and a test that checks one code path is not much better — the
improvement cycle in particular exists to *propose* changes, so the temptation
to apply one is built into its job description.

So it is enforced at runtime. Both cycles run inside `readonly_config()`, which
replaces `save`, `delete` and the queryset writers on the models that hold
strategy and risk settings with functions that raise. A reviewer that tries to
tune a parameter crashes its own run and files a finding about itself, which is
loud, reversible, and impossible to miss — the three properties the silent
version lacks.

Deliberately NOT protected: the review models themselves, JournalEntry, and
FeedEvent. The reviewer has to be able to record what it found.
"""
from __future__ import annotations

import contextlib

# Everything that decides what gets traded, how much, or with whose money.
PROTECTED = ('Strategy', 'AgentConfig', 'Instrument')


class ReviewerWroteToConfig(RuntimeError):
    """A review cycle tried to change something it is not allowed to change."""


def _blocked(model_name: str, op: str):
    def boom(*a, **k):
        raise ReviewerWroteToConfig(
            f'the reviewer tried to {op} {model_name}. Review cycles may propose changes and '
            f'may not make them: strategy settings, risk limits and live arming are the owner\'s.')
    return boom


@contextlib.contextmanager
def readonly_config():
    """Make the strategy/risk/instrument tables unwritable for the duration."""
    from main_app import models as M
    from django.db.models import QuerySet

    saved = []
    for name in PROTECTED:
        model = getattr(M, name)
        saved.append((model, model.save, model.delete))
        model.save = _blocked(name, 'save')
        model.delete = _blocked(name, 'delete')
    qs_saved = (QuerySet.update, QuerySet.delete, QuerySet.bulk_update)

    def guarded(original, op):
        def inner(self, *a, **k):
            if self.model.__name__ in PROTECTED:
                _blocked(self.model.__name__, op)()
            return original(self, *a, **k)
        return inner

    QuerySet.update = guarded(qs_saved[0], 'update')
    QuerySet.delete = guarded(qs_saved[1], 'delete')
    QuerySet.bulk_update = guarded(qs_saved[2], 'bulk_update')
    try:
        yield
    finally:
        for model, save, delete in saved:
            model.save, model.delete = save, delete
        QuerySet.update, QuerySet.delete, QuerySet.bulk_update = qs_saved

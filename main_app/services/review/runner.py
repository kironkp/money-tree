"""Executing a review cycle: run the checks, act, record, alert.

The runner owns three things the checks deliberately do not: the read-only
guard, the halt decision, and the run row. A check that could halt trading on
its own would make the halt policy a property of nine scattered functions.
"""
from __future__ import annotations

import logging
import traceback

from datetime import timedelta

from django.utils import timezone

from main_app.models import Account, Hypothesis, ReviewFinding, ReviewRun
from .findings import Recorder, alert
from .guard import readonly_config

log = logging.getLogger(__name__)


def run_operational(accounts=None, trigger: str = 'schedule', broker=None,
                    now=None, notify: bool = True, next_due_at=None) -> ReviewRun:
    """One operational pass over one or more lanes.

    `broker` is the live handle when the agent calls this in-process after a
    fill; None when the scheduled job runs it out of process, in which case the
    broker-facing checks record that they were skipped rather than passing
    silently. A skipped check must never look like a passed one.
    """
    from . import operational as ops

    now = now or timezone.now()
    run = ReviewRun.objects.create(cycle=ReviewRun.OPERATIONAL, trigger=trigger,
                                   started_at=now, next_due_at=next_due_at)
    rec = Recorder(run)
    accounts = list(accounts if accounts is not None else reviewable_accounts())
    ran, failed, notes, skipped = [], [], [], set()
    try:
        with readonly_config():
            for account in accounts:
                ctx = ops.Ctx(account=account, now=now, broker=broker)
                for name, fn in ops.CHECKS.items():
                    try:
                        fn(ctx, rec)
                        if name not in ran:
                            ran.append(name)
                    except Exception as exc:
                        failed.append(name)
                        log.exception('operational check %s failed on %s', name, account.market)
                        rec.record('check_crashed', ReviewFinding.CRITICAL,
                                   f'the {name} check crashed',
                                   f'{exc!r}\n\n{traceback.format_exc()[-1200:]}',
                                   account=account, evidence={'check': name, 'error': repr(exc)},
                                   fp_parts=('check_crashed', account.pk, name))
                notes += [f'{account.market}: {n}' for n in ctx.notes]
                skipped |= ctx.skipped

            # Only checks that actually LOOKED may close findings.
            #
            # Two ways a check can fail to look, and both were being treated as
            # a clean pass. Crashing was already handled. Returning early was
            # not: the scheduled job runs with no broker handle, so
            # position_vs_broker returns immediately, and its silence was closing
            # the divergence finding the in-process reviewer had just raised —
            # with a resolution line claiming the check "ran clean" — and then
            # lifting the halt. The lane resumed trading against books that still
            # disagreed with the venue. Same shape for stale_data every time the
            # market closes.
            looked = [n for n in ran if n not in failed and n not in skipped]
            closable = [k for name in looked for k in ops.RAISES.get(name, [])]
            closed = rec.sweep_resolved(closable, accounts)

            halting = [f for f in rec.opened + rec.repeated
                       if f.severity == ReviewFinding.CRITICAL and f.check_key in ops.HALTING_CHECKS]
            for account in accounts:
                mine = [f for f in halting if f.account_id == account.pk]
                if mine:
                    rec.halt(account, '; '.join(f.title for f in mine[:3])[:300])
                elif account.review_halt:
                    # The reviewer opened this halt, so the reviewer closes it —
                    # but only once every halting finding on the lane is gone.
                    still = ReviewFinding.objects.filter(
                        account=account, status=ReviewFinding.OPEN,
                        severity=ReviewFinding.CRITICAL,
                        check_key__in=ops.HALTING_CHECKS).exists()
                    if not still:
                        rec.clear_halt(account, 'no halting findings remain')
        rec.commit()
        run.status = 'failed' if failed else 'ok'
        run.checks_run = len(ran)
        run.checks_failed = len(failed)
        run.summary = {'accounts': [a.market for a in accounts], 'checks': ran, 'crashed': failed,
                       'skipped': sorted(skipped), 'skipped_notes': notes,
                       'resolved': [f.check_key for f in closed]}
    except Exception as exc:                       # the runner itself broke
        run.status = 'failed'
        run.error = f'{exc!r}\n{traceback.format_exc()[-2000:]}'
        log.exception('operational review runner failed')
    finally:
        run.finished_at = timezone.now()
        run.save()
    if notify:
        # New findings always, and a standing critical only when it is stale
        # enough to be worth saying again. Mailing every repeated critical meant
        # one unfixed fault sent 96 messages a day from the 15-minute job alone,
        # which is how an operator learns to filter the alerts.
        alert(run, rec.opened + [f for f in rec.repeated
                                 if f.severity == ReviewFinding.CRITICAL and _worth_repeating(f, now)])
    return run


def run_improvement(accounts=None, trigger: str = 'schedule', now=None,
                    notify: bool = True, next_due_at=None) -> ReviewRun:
    """The daily read of whether the strategies are any good.

    Separate run, separate cycle, same read-only guard. It may open findings and
    record hypotheses; it may not touch a strategy, a risk limit or the live
    switch, and `readonly_config` makes that a crash rather than a promise.
    """
    from . import improvement as imp

    now = now or timezone.now()
    run = ReviewRun.objects.create(cycle=ReviewRun.IMPROVEMENT, trigger=trigger,
                                   started_at=now, next_due_at=next_due_at)
    rec = Recorder(run)
    accounts = list(accounts if accounts is not None else reviewable_accounts())
    reads, ran, failed = {}, [], []
    try:
        with readonly_config():
            for account in accounts:
                try:
                    reads[account.market] = imp.analyse(account, rec, now=now)
                    ran.append(account.market)
                except Exception as exc:
                    failed.append(account.market)
                    log.exception('improvement analysis failed on %s', account.market)
                    rec.record('analysis_crashed', ReviewFinding.CRITICAL,
                               f'the improvement analysis crashed on {account.market}', repr(exc),
                               account=account, fp_parts=('analysis_crashed', account.pk))
            if not failed:
                rec.sweep_resolved(['mistake_pattern', 'strategy_losing', 'cost_dominates'], accounts)
        rec.commit()
        run.status = 'failed' if failed else 'ok'
        run.checks_run = len(ran)
        run.checks_failed = len(failed)
        run.summary = {
            'analysed': ran, 'crashed': failed,
            'net_after_costs': {m: {'net': r['costs']['net'], 'gross': r['costs']['gross'],
                                    'cost_share': r['costs']['cost_share'],
                                    'trades': r['costs']['trades']}
                                for m, r in reads.items()},
            'open_hypotheses': list(Hypothesis.objects.exclude(
                status__in=(Hypothesis.REJECTED, Hypothesis.ACCEPTED))
                .values_list('title', flat=True)[:10]),
        }
    except Exception as exc:
        run.status = 'failed'
        run.error = f'{exc!r}\n{traceback.format_exc()[-2000:]}'
        log.exception('improvement review runner failed')
    finally:
        run.finished_at = timezone.now()
        run.save()
    if notify:
        alert(run, [f for f in rec.opened if f.severity in (ReviewFinding.CRITICAL, ReviewFinding.WARN)])
    return run


REMIND_AFTER = timedelta(hours=6)


def _worth_repeating(finding, now) -> bool:
    """A standing critical is worth one reminder every few hours, not every run."""
    last = finding.first_seen_at
    elapsed = (now - last).total_seconds()
    return int(elapsed // REMIND_AFTER.total_seconds()) > int(
        (elapsed - 1) // REMIND_AFTER.total_seconds()) or finding.seen_count == 2


def reviewable_accounts():
    """Every account that can hold a position, not just the simulated ones.

    Defaulting to mode='sim' meant the scheduled job never looked at a paper or
    live lane — so the reconciliation checks, which return early for anything
    that is not paper or live, were unreachable dead code, and the two halting
    findings they raise could never fire. The review subsystem existed to make
    live trading safe and was pointed exclusively at the one mode that is not.
    """
    return Account.objects.exclude(mode='replay').order_by('mode', 'market')


def last_run(cycle: str) -> ReviewRun | None:
    return ReviewRun.objects.filter(cycle=cycle).order_by('-started_at').first()


def cycle_status(cycle: str) -> dict:
    """Everything the app needs to show about a cycle without knowing its schedule."""
    run = last_run(cycle)
    from .findings import open_findings
    opens = open_findings(cycle)
    return {
        'cycle': cycle,
        'last_run': run,
        'last_status': run.status if run else 'never run',
        'last_at': run.started_at if run else None,
        'next_due_at': run.next_due_at if run else None,
        'checks_run': run.checks_run if run else 0,
        'checks_failed': run.checks_failed if run else 0,
        'actions': (run.actions if run else []) or [],
        'open_total': opens.count(),
        'open_critical': opens.filter(severity=ReviewFinding.CRITICAL).count(),
        'findings': list(opens.order_by('-severity', '-last_seen_at')[:20]),
        'blockers': list(Account.objects.filter(review_halt=True)
                         .values_list('market', 'review_halt_reason')),
    }

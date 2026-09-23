"""Recording what a review cycle found, once, with evidence.

Three things make this more than a log table.

Deduplication. A reviewer that runs every fifteen minutes will see one unfixed
fault ninety-six times a day. Each finding carries a `fingerprint` built from
the things that identify the *problem* rather than the *sighting*, so a repeat
bumps a counter instead of writing a row. Restarts are therefore free: the
reviewer comes back up, sees the same fault, and updates the row it already has.

Resolution by re-check. A finding closes when the check that raised it stops
raising it, not when someone declares it fixed. `sweep_resolved` is what turns
"a fix was attempted" into "the fix worked".

Halting. A critical finding sets a durable, per-lane block on NEW entries. It
does not flatten: abandoning a live stop because reconciliation is stale is a
worse trade than the one that tripped the check.
"""
from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from main_app.models import Account, FeedEvent, ReviewFinding, ReviewRun

log = logging.getLogger(__name__)


def fingerprint(*parts) -> str:
    """Identify the problem, not the sighting.

    Deliberately excludes timestamps and run ids: including them would make
    every sighting unique, which is the bug this function exists to prevent.
    """
    raw = '|'.join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class Recorder:
    """Collects findings for one run, then commits them together."""

    def __init__(self, run: ReviewRun):
        self.run = run
        self.opened: list[ReviewFinding] = []
        self.repeated: list[ReviewFinding] = []
        self.seen_keys: set[tuple] = set()
        self.actions: list[str] = []

    def record(self, check_key: str, severity: str, title: str, detail: str = '',
               account: Account | None = None, evidence: dict | None = None,
               fp_parts: tuple | None = None) -> ReviewFinding:
        fp = fingerprint(*(fp_parts if fp_parts is not None else (check_key, account.pk if account else '', title)))
        now = timezone.now()
        self.seen_keys.add((self.run.cycle, check_key, fp))
        row, created = ReviewFinding.objects.get_or_create(
            cycle=self.run.cycle, check_key=check_key, fingerprint=fp,
            defaults={'severity': severity, 'account': account, 'title': title[:200],
                      'detail': detail, 'evidence': evidence or {}, 'first_run': self.run,
                      'last_run': self.run, 'first_seen_at': now, 'last_seen_at': now})
        if created:
            self.opened.append(row)
            return row
        # A repeat. Reopen if it had been resolved — the fault came back, and a
        # fault that comes back is worth more attention than a new one, not less.
        row.last_seen_at = now
        row.last_run = self.run
        row.seen_count += 1
        row.severity = severity
        row.detail = detail
        row.evidence = evidence or {}
        fields = ['last_seen_at', 'last_run', 'seen_count', 'severity', 'detail', 'evidence']
        if row.status == ReviewFinding.RESOLVED:
            row.status = ReviewFinding.OPEN
            row.resolved_at = None
            row.resolution = f'reopened — seen again {now:%Y-%m-%d %H:%M}'
            fields += ['status', 'resolved_at', 'resolution']
        row.save(update_fields=fields)
        self.repeated.append(row)
        return row

    # --- what the reviewer DID, as opposed to what it saw -------------------
    def halt(self, account: Account, reason: str) -> None:
        """Block new entries on one lane, durably. Open positions stay managed."""
        if account.review_halt:
            return
        account.review_halt = True
        account.review_halt_reason = reason[:300]
        account.review_halt_at = timezone.now()
        account.save(update_fields=['review_halt', 'review_halt_reason', 'review_halt_at'])
        self.actions.append(f'halted new entries on {account.market}: {reason}')
        log.warning('review halt on %s: %s', account.market, reason)

    def clear_halt(self, account: Account, why: str) -> None:
        if not account.review_halt:
            return
        account.review_halt = False
        account.review_halt_reason = ''
        account.review_halt_at = None
        account.save(update_fields=['review_halt', 'review_halt_reason', 'review_halt_at'])
        self.actions.append(f'cleared the halt on {account.market}: {why}')

    def sweep_resolved(self, checks_run: list[str], accounts=None) -> list[ReviewFinding]:
        """Close open findings whose check looked this time and did not re-raise.

        Scoped two ways, and both matter.

        By check: one that crashed or returned early must not be allowed to close
        the findings it was supposed to re-confirm.

        By ACCOUNT: the caller reviews a subset of lanes — the in-process
        reviewer passes exactly one — and without this every lane's review
        erased the other three lanes' open findings, because their checks had
        'run' in this pass and not re-raised for lanes they never looked at.
        Four agents reviewing themselves every five minutes meant the desk
        continuously deleted its own evidence.
        """
        stale = ReviewFinding.objects.filter(cycle=self.run.cycle, check_key__in=checks_run,
                                             status=ReviewFinding.OPEN)
        if accounts is not None:
            ids = [a.pk for a in accounts]
            stale = stale.filter(account_id__in=ids)
        closed = []
        for row in stale:
            if (self.run.cycle, row.check_key, row.fingerprint) in self.seen_keys:
                continue
            row.status = ReviewFinding.RESOLVED
            row.resolved_at = timezone.now()
            row.resolution = f'the {row.check_key} check ran clean at {row.resolved_at:%Y-%m-%d %H:%M}'
            row.save(update_fields=['status', 'resolved_at', 'resolution'])
            closed.append(row)
        return closed

    def commit(self) -> None:
        self.run.findings_opened = len(self.opened)
        self.run.findings_repeated = len(self.repeated)
        self.run.actions = self.actions


def open_findings(cycle: str | None = None, severity: str | None = None):
    q = ReviewFinding.objects.filter(status=ReviewFinding.OPEN)
    if cycle:
        q = q.filter(cycle=cycle)
    if severity:
        q = q.filter(severity=severity)
    return q.select_related('account')


def alert(run: ReviewRun, findings: list[ReviewFinding]) -> bool:
    """Email the owner. Never raises into the caller — a failed alert is itself
    a finding, and a reviewer that dies trying to complain is worse than one
    that complains badly."""
    if not findings:
        return False
    for f in findings:
        FeedEvent.objects.create(
            account=f.account, level=('error' if f.severity == ReviewFinding.CRITICAL else 'risk'),
            phase='alert', text=f'REVIEW [{f.severity}] {f.title}'[:400],
            symbol=(f.evidence or {}).get('symbol', '') or '', ts=timezone.now())
    to = getattr(settings, 'REPORT_EMAIL', '') or getattr(settings, 'DEFAULT_TO_EMAIL', '')
    if not to or not settings.EMAIL_HOST:
        log.warning('review alert not emailed (no recipient or mail host): %d findings', len(findings))
        return False
    crit = [f for f in findings if f.severity == ReviewFinding.CRITICAL]
    subject = (f'MoneyTree {run.cycle} review: {len(crit)} critical, {len(findings) - len(crit)} other'
               if crit else f'MoneyTree {run.cycle} review: {len(findings)} findings')
    lines = [f'{run.cycle} review, {run.started_at:%Y-%m-%d %H:%M}', '']
    for f in findings:
        lines += [f'[{f.severity.upper()}] {f.title}',
                  f'  lane: {f.account.market if f.account else "desk"}   seen {f.seen_count}x   check: {f.check_key}',
                  f'  {f.detail}', '']
    if run.actions:
        lines += ['Actions taken:'] + [f'  - {a}' for a in run.actions] + ['']
    lines.append('Nothing here changed a strategy or a risk limit. Review cycles may only propose.')
    try:
        EmailMultiAlternatives(subject, '\n'.join(lines), settings.DEFAULT_FROM_EMAIL, [to]).send(fail_silently=False)
        return True
    except Exception as exc:
        log.error('review alert email failed: %r', exc)
        return False

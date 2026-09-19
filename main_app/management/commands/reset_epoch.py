"""Give the lanes a clean scoreboard without giving them amnesia.

The money resets. The record does not. Not one trade is deleted, because the
lifetime brake in `promotion.lifetime_verdict` counts every trade a strategy has
ever taken, and that brake is the only thing standing between this desk and a
losing idea running forever thirty trades at a time. `evidence_since` already
taught that lesson expensively: resetting a clock erased burst's earned
quarantine and the lane lost another $1,077.52 before anyone noticed.

So a reset moves the starting line and nothing else. Reports show the epoch by
default, so the current setup is judged on its own results; the brake keeps
seeing all 450 trades and stays armed.

Refuses to run while a position is open — a reset mid-trade would book the
remainder of that trade against a starting balance it was never opened from.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from main_app.models import Account, JournalEntry, Mode, Position, Trade


class Command(BaseCommand):
    help = 'Reset lane balances to their seed capital and start a new scoring epoch'

    def add_arguments(self, parser):
        parser.add_argument('--mode', default='sim', choices=['sim', 'paper', 'live'])
        parser.add_argument('--markets', default='', help='comma-separated (default: all)')
        parser.add_argument('--note', default='', help='why, for the record')
        parser.add_argument('--apply', action='store_true', help='without this it only shows the plan')
        parser.add_argument('--force', action='store_true',
                            help='reset even with agents running (restart them immediately after)')

    def handle(self, *args, **o):
        accounts = Account.objects.filter(mode=o['mode']).order_by('market')
        if o['markets']:
            wanted = [m.strip() for m in o['markets'].split(',') if m.strip()]
            accounts = accounts.filter(market__in=wanted)
        if not accounts:
            self.stderr.write('no matching accounts')
            return

        if o['mode'] == Mode.LIVE:
            self.stderr.write('refusing to reset a live account from a command')
            return

        # A running agent holds its broker in memory, hydrated from the ledger at
        # startup, and writes that state back every tick. Reset underneath one and
        # it simply puts the old balance back a few seconds later — which is
        # exactly what happened the first time this ran: cash reset on all four
        # lanes, equity silently restored on two.
        from main_app.services import control
        live = [r.market for r in control.running_agents()
                if r.mode == o['mode'] and r.market in {a.market for a in accounts}]
        if live and not o['apply']:
            self.stdout.write(f'note: agents running for {", ".join(sorted(live))} — '
                              'they will be stopped, reset, and restarted in that order')

        open_pos = Position.objects.filter(account__in=accounts).count()
        if open_pos:
            self.stderr.write(f'{open_pos} position(s) still open — flatten first, or the rest of '
                              'those trades books against a balance they were never opened from')
            return

        now = timezone.now()
        rows = []
        for a in accounts:
            trades = Trade.objects.filter(account=a)
            since = trades.filter(exit_ts__gte=a.epoch_started_at) if a.epoch_started_at else trades
            rows.append({
                'account': a, 'market': a.market,
                'equity': float(a.equity), 'seed': float(a.starting_cash),
                'pnl': float(a.equity) - float(a.starting_cash),
                'trades_all': trades.count(), 'trades_epoch': since.count(),
                'fees_all': float(sum(t.fees for t in trades)) if trades.exists() else 0.0,
            })

        self.stdout.write(f'{"lane":9s}{"equity":>12s}{"-> seed":>12s}{"closing P&L":>14s}'
                          f'{"trades kept":>13s}')
        for r in rows:
            self.stdout.write(f'{r["market"]:9s}{r["equity"]:>12,.2f}{r["seed"]:>12,.2f}'
                              f'{r["pnl"]:>+14,.2f}{r["trades_all"]:>13d}')
        total = sum(r['pnl'] for r in rows)
        self.stdout.write(f'{"TOTAL":9s}{"":>12s}{"":>12s}{total:>+14,.2f}'
                          f'{sum(r["trades_all"] for r in rows):>13d}')

        if not o['apply']:
            self.stdout.write(self.style.WARNING('\ndry run — pass --apply to do it'))
            self.stdout.write('no trade is deleted; the lifetime brake keeps seeing all of them')
            return

        note = o['note'] or 'manual reset'

        # STOP FIRST, and this order is not negotiable. A running agent holds its
        # broker in memory and its SHUTDOWN path persists that state — so
        # resetting while one is alive, or using restart_agents (which stops and
        # starts in one call), writes the old balance straight back over the new
        # one. That happened twice before this guard existed: cash reset on four
        # lanes, equity silently restored on two, and the report then disagreed
        # with itself.
        import time

        from main_app.services import procs
        stopped = []
        for run in control.running_agents():
            if run.mode != o['mode'] or run.market not in {a.market for a in accounts}:
                continue
            procs.stop(run.pid)
            for _ in range(60):
                if not procs.alive(run.pid):
                    break
                time.sleep(1)
            if procs.alive(run.pid):
                self.stderr.write(f'{run.market}: pid {run.pid} would not stop — aborting, '
                                  'nothing has been reset')
                return
            stopped.append(run.market)
            self.stdout.write(f'stopped {run.market} (pid {run.pid})')

        with transaction.atomic():
            for r in rows:
                a = r['account']
                a.cash = Decimal(str(a.starting_cash))
                a.equity = Decimal(str(a.starting_cash))
                a.epoch_started_at = now
                a.epoch_note = note[:200]
                a.day_halted = False
                a.day_halted_reason = ''
                a.day_entries = 0
                a.save(update_fields=['cash', 'equity', 'epoch_started_at', 'epoch_note',
                                      'day_halted', 'day_halted_reason', 'day_entries'])
            JournalEntry.objects.create(
                date=timezone.localdate(), kind='reset',
                title=f'Scoring epoch reset — closing P&L {total:+,.2f}',
                body='\n'.join(
                    [f'Reset at {now:%Y-%m-%d %H:%M} — {note}', '',
                     'Balances returned to seed capital. No trade was deleted: the lifetime brake '
                     'still counts every one, so a strategy that has already earned a quarantine '
                     'cannot buy a second life from a fresh scoreboard.', ''] +
                    [f'  {r["market"]:8s} {r["equity"]:>10,.2f} -> {r["seed"]:>10,.2f}  '
                     f'({r["pnl"]:+,.2f} over {r["trades_all"]} trades, {r["fees_all"]:,.2f} fees)'
                     for r in rows]),
                metrics={'closing_pnl': total, 'lanes': [
                    {k: v for k, v in r.items() if k != 'account'} for r in rows]})
        self.stdout.write(self.style.SUCCESS(f'\nreset {len(rows)} lane(s); epoch starts {now:%H:%M}'))
        self.stdout.write('every trade kept — the brake is still armed')

        for market in stopped:
            pid = procs.spawn_manage(['run_agent', '--mode', o['mode'], '--market', market],
                                     f'agent-{o["mode"]}-{market}')
            self.stdout.write(f'restarted {market} (pid {pid})')

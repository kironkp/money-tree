"""Where the research arm stands against the preregistered gate."""
from django.core.management.base import BaseCommand

from main_app.models import Evaluation
from main_app.services.dossier import grade
from main_app.services.evaluation import assess, decide
from main_app.services.preregistration import current, describe


class Command(BaseCommand):
    help = 'Grade the shadow trades and report against the preregistered criterion.'

    def add_arguments(self, parser):
        parser.add_argument('--decide', action='store_true',
                            help='record a checkpoint decision if one is due')
        parser.add_argument('--prereg', action='store_true', help='print the frozen experiment')

    def handle(self, *a, **o):
        ev = current()
        if o['prereg']:
            self.stdout.write(describe(ev))
            return

        g = grade()
        if g['graded']:
            self.stdout.write(f"graded {g['graded']} shadow trade(s)")

        out = decide(ev, apply=o['decide'])
        a_ = out['assessment']
        self.stdout.write(f"\n{ev.identifier}  {a_['n_days']} trading days collected")
        if a_['sigma_lr']:
            self.stdout.write(f"  long-run sigma {a_['sigma_lr']:.3f} ATR/day -> "
                              f"{a_['required_days']} days needed for {1 - ev.beta:.0%} power "
                              f"at a {ev.delta_min:+.2f} hurdle")
        else:
            self.stdout.write('  not enough days to estimate the variance yet')
        if a_['primary']:
            p = a_['primary']
            self.stdout.write(f"  research - headline: {p['mean']:+.3f} ATR/day "
                              f"[{p['lower']:+.3f}, {p['upper']:+.3f}] at alpha "
                              f"{a_['alpha_spent_next']:.4f}")
        mix = a_['outcomes']
        self.stdout.write(f"  shadow trades: {mix.get('target', 0)} target, {mix.get('stop', 0)} stop, "
                          f"{mix.get('timeout', 0)} timeout, {mix.get('vetoed', 0)} vetoed")
        for name, f in (a_['forecasts'] or {}).items():
            if f['brier']:
                skill = f['brier']['skill']
                self.stdout.write(f"  {name}: Brier {f['brier']['brier']:.4f} over "
                                  f"{f['brier']['n']}, skill "
                                  f"{skill:.3f}" if skill is not None else '  —')
        style = self.style.SUCCESS if out['decision'] == 'promote' else self.style.WARNING
        self.stdout.write(style(f"\n{out['decision'].upper()}: {out['reason']}"))
        if not o['decide'] and out['decision'] in ('promote', 'demote'):
            self.stdout.write('  (run with --decide to record it)')
        closed = Evaluation.objects.exclude(status='collecting').count()
        if closed:
            self.stdout.write(f'\n{closed} closed evaluation(s) in the history')

from django.test import SimpleTestCase

from main_app.management.commands.auto_research import comparable_verdict, evidence_passes


def metrics(trades=20, profit_factor=1.2, net_pnl=100, expectancy=5):
    return {'trades': trades, 'profit_factor': profit_factor, 'net_pnl': net_pnl, 'expectancy': expectancy}


class ResearchEvidenceGate(SimpleTestCase):
    def test_losing_current_params_are_not_called_confirmed(self):
        verdict, promote = comparable_verdict({'fast': 9}, {'fast': 9},
                                               metrics(profit_factor=.94, net_pnl=-10, expectancy=-.5),
                                               metrics(profit_factor=.94, net_pnl=-10, expectancy=-.5))
        self.assertFalse(promote)
        self.assertIn('NOT confirmed', verdict)

    def test_current_params_need_enough_held_out_trades(self):
        verdict, promote = comparable_verdict({'fast': 9}, {'fast': 9}, metrics(trades=9), metrics(trades=9))
        self.assertFalse(promote)
        self.assertIn('NOT confirmed', verdict)

    def test_profitable_current_params_can_be_confirmed_without_promotion(self):
        verdict, promote = comparable_verdict({'fast': 9}, {'fast': 9}, metrics(), metrics())
        self.assertFalse(promote)
        self.assertIn('confirmed current params', verdict)

    def test_candidate_must_clear_cost_adjusted_gate_and_beat_champion(self):
        verdict, promote = comparable_verdict({'fast': 11}, {'fast': 9}, metrics(profit_factor=1.4, net_pnl=120),
                                               metrics(profit_factor=1.2, net_pnl=100))
        self.assertTrue(promote)
        self.assertTrue(verdict.startswith('PROMOTE:'))
        self.assertFalse(evidence_passes(metrics(profit_factor=1.5, net_pnl=-1, expectancy=-.1)))

    def test_candidate_with_better_pf_but_worse_net_is_not_promoted(self):
        verdict, promote = comparable_verdict({'fast': 11}, {'fast': 9}, metrics(profit_factor=1.4, net_pnl=90),
                                               metrics(profit_factor=1.2, net_pnl=100))
        self.assertFalse(promote)
        self.assertIn('kept current params', verdict)

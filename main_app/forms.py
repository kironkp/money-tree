from allauth.account.forms import SignupForm
from django import forms

from .models import AgentConfig, Instrument, Mode


class InviteSignupForm(SignupForm):
    """allauth's signup form, gated: the email must be invited (or on the
    always-allowed list)."""

    def clean_email(self):
        email = super().clean_email()
        from .adapters import signup_allowed
        if not signup_allowed(email):
            raise forms.ValidationError('MoneyTree is invite-only. Ask the owner for an invite for this address.')
        return email


class InviteForm(forms.Form):
    email = forms.EmailField()
    note = forms.CharField(max_length=120, required=False)
    make_operator = forms.BooleanField(required=False)


class AgentConfigForm(forms.ModelForm):
    class Meta:
        model = AgentConfig
        fields = [
            'trading_enabled', 'timeframe', 'crypto_timeframe', 'degen_timeframe', 'pulse_seconds', 'starting_cash', 'risk_per_trade_pct', 'max_position_pct',
            'max_open_positions', 'max_daily_loss_pct', 'max_trades_per_day', 'no_entries_before_close_min',
            'flat_before_close_min', 'max_hold_minutes', 'slippage_bps', 'fee_bps_stock',
            'fee_bps_crypto', 'liquidity_cap_pct', 'min_reward_to_cost', 'live_confirm_orders', 'live_confirm_minutes',
            'degen_risk_per_trade_pct', 'degen_max_position_pct', 'degen_max_open_positions', 'degen_max_daily_loss_pct',
            'degen_max_trades_per_day', 'degen_max_hold_minutes', 'degen_min_reward_to_cost',
            'forex_timeframe', 'forex_leverage', 'forex_risk_per_trade_pct', 'forex_max_position_pct', 'forex_max_open_positions',
            'forex_max_daily_loss_pct', 'forex_max_trades_per_day', 'forex_max_hold_minutes', 'forex_min_reward_to_cost',
            'forex_slippage_bps', 'fee_bps_forex',
        ]
        widgets = {'timeframe': forms.Select(choices=[(t, t) for t in ('1Min', '5Min', '15Min', '30Min', '1Hour')]),
                   'crypto_timeframe': forms.Select(choices=[(t, t) for t in ('5Min', '15Min', '30Min', '1Hour')]),
                   'degen_timeframe': forms.Select(choices=[(t, t) for t in ('1Min', '5Min', '15Min')]),
                   'forex_timeframe': forms.Select(choices=[(t, t) for t in ('1Min', '5Min', '15Min', '30Min', '1Hour')])}


class InstrumentForm(forms.ModelForm):
    class Meta:
        model = Instrument
        fields = ['symbol', 'name', 'asset_class', 'qty_increment']

    def clean_symbol(self):
        return self.cleaned_data['symbol'].strip().upper()


class JournalForm(forms.Form):
    date = forms.DateField()
    title = forms.CharField(max_length=120)
    body = forms.CharField(widget=forms.Textarea, required=False)


def strategy_param_form(cls, initial: dict | None = None):
    """Build a form class from a strategy's params schema."""
    fields = {}
    for p in cls.params:
        kw = {'label': p.name.replace('_', ' '), 'help_text': p.help, 'required': False,
              'initial': (initial or {}).get(p.name, p.default)}
        if p.type == 'int':
            fields[p.name] = forms.IntegerField(min_value=p.min, max_value=p.max, **kw)
        elif p.type == 'float':
            fields[p.name] = forms.FloatField(min_value=p.min, max_value=p.max, **kw)
        elif p.type == 'bool':
            fields[p.name] = forms.BooleanField(**kw)
        else:
            fields[p.name] = forms.ChoiceField(choices=[(c, c) for c in p.choices], **kw)
    return type(f'{cls.__name__}ParamForm', (forms.Form,), fields)

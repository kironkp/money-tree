from django import forms

from .models import AgentConfig, Instrument, Mode


class AgentConfigForm(forms.ModelForm):
    class Meta:
        model = AgentConfig
        fields = [
            'trading_enabled', 'timeframe', 'starting_cash', 'risk_per_trade_pct', 'max_position_pct',
            'max_open_positions', 'max_daily_loss_pct', 'max_trades_per_day', 'no_entries_before_close_min',
            'flat_before_close_min', 'allow_short', 'max_hold_minutes', 'slippage_bps', 'fee_bps_stock',
            'fee_bps_crypto', 'liquidity_cap_pct',
        ]
        widgets = {'timeframe': forms.Select(choices=[(t, t) for t in ('1Min', '5Min', '15Min')])}


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

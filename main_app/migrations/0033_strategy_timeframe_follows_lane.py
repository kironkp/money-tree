"""A strategy's timeframe: '' follows the lane's base and moves with it.

Strategy.timeframe used to mirror the lane's timeframe (an edit overwrote it).
Since v1.73 it is the strategy's own: '' means "the lane's base, whatever it
becomes", and only a deliberate coarser choice is stored explicitly. Rows equal
to their lane's current base are blanked so a later base change carries them.
"""
from django.db import migrations, models

LANE_FIELD = {'crypto': 'crypto_timeframe', 'degen': 'degen_timeframe', 'forex': 'forex_timeframe'}


def _base(cfg, market):
    return getattr(cfg, LANE_FIELD.get(market, 'timeframe'))


def follow_lane(apps, schema_editor):
    AgentConfig = apps.get_model('main_app', 'AgentConfig')
    Strategy = apps.get_model('main_app', 'Strategy')
    cfg = AgentConfig.objects.order_by('pk').first()
    if cfg is None:
        return
    for row in Strategy.objects.all():
        if row.timeframe == _base(cfg, row.market):
            row.timeframe = ''
            row.save(update_fields=['timeframe'])


def pin_to_lane(apps, schema_editor):
    AgentConfig = apps.get_model('main_app', 'AgentConfig')
    Strategy = apps.get_model('main_app', 'Strategy')
    cfg = AgentConfig.objects.order_by('pk').first()
    if cfg is None:
        return
    for row in Strategy.objects.filter(timeframe=''):
        row.timeframe = _base(cfg, row.market)
        row.save(update_fields=['timeframe'])


class Migration(migrations.Migration):

    dependencies = [
        ('main_app', '0032_experiment_strategy_timeframe'),
    ]

    operations = [
        migrations.AddField(
            model_name='backtestrun',
            name='strategy_timeframe',
            field=models.CharField(blank=True, default='', max_length=8),
        ),
        migrations.AlterField(
            model_name='strategy',
            name='timeframe',
            field=models.CharField(blank=True, default='', max_length=8),
        ),
        migrations.RunPython(follow_lane, pin_to_lane),
    ]

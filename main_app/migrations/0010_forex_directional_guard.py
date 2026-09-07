from decimal import Decimal

from django.db import migrations, models


def park_unresearched_forex_defaults(apps, schema_editor):
    """Stop default FX ideas that were enabled without promotion evidence.

    A user-promoted version has either a bumped version or history, so it is
    deliberately left alone.
    """
    Strategy = apps.get_model('main_app', 'Strategy')
    rows = Strategy.objects.filter(
        market='forex', qualification='unproven', version=1, enabled=True,
    )
    for row in rows:
        if row.history:
            continue
        row.enabled = False
        row.stage = 'seed'
        row.qualification_reason = (
            'parked: default parameters have no validated held-out evidence; '
            'run research and promote a passing candidate before observation'
        )
        row.save(update_fields=['enabled', 'stage', 'qualification_reason'])


class Migration(migrations.Migration):

    dependencies = [
        ('main_app', '0009_strategy_qualification'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentconfig',
            name='forex_max_directional_exposure_pct',
            field=models.DecimalField(decimal_places=2, default=Decimal('500'), max_digits=7),
        ),
        migrations.RunPython(park_unresearched_forex_defaults, migrations.RunPython.noop),
    ]

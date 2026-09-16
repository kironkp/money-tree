"""Every verdict that already existed is `reconstructed`, not evidence.

The 266 rows written before this migration were scored without a recorded
price, graded (or rather not graded) against a +24h window the desk never
traded, and judged under an act threshold that has since moved. Their outcomes
can only be rebuilt after the fact from bars.

That makes them useful — the score distribution is real, and they are the only
data the grader can be debugged against — and it makes them unusable as
experimental evidence. Marking them here, once, is what stops a later
scoreboard from quietly pooling them with forecasts that were actually
recorded before the fact.
"""
from django.db import migrations


def mark(apps, schema_editor):
    apps.get_model('main_app', 'NewsVerdict').objects.update(provenance='reconstructed')


def unmark(apps, schema_editor):
    apps.get_model('main_app', 'NewsVerdict').objects.update(provenance='contemporaneous')


class Migration(migrations.Migration):
    dependencies = [('main_app', '0019_phase0_evidence')]
    operations = [migrations.RunPython(mark, unmark)]

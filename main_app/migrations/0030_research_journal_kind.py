"""Auto-research journal rows get their own kind.

They were written as kind='auto_eod', the kind the end-of-day journal uses both
as its update_or_create key and as the agent's "today is already journalled"
marker. Every lane restarted after the 02:10 research run therefore skipped its
journal, and a journal that did run raised MultipleObjectsReturned. Relabel
only: nothing is deleted, and the title is the match because the research rows
are the only ones that start with "Auto-research".
"""
from django.db import migrations


def relabel(apps, schema_editor):
    JournalEntry = apps.get_model('main_app', 'JournalEntry')
    JournalEntry.objects.filter(kind='auto_eod', title__startswith='Auto-research').update(kind='research')


def unlabel(apps, schema_editor):
    JournalEntry = apps.get_model('main_app', 'JournalEntry')
    JournalEntry.objects.filter(kind='research', title__startswith='Auto-research').update(kind='auto_eod')


class Migration(migrations.Migration):

    dependencies = [
        ('main_app', '0029_accounting_ledger'),
    ]

    operations = [
        migrations.RunPython(relabel, unlabel),
    ]

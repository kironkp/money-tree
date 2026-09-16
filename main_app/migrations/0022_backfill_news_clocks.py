"""Give the existing headlines the clock the new ones will carry.

`first_public_at` is the only timestamp a freshness or latency claim may be made
against, so every row needs one. For rows ingested before this migration the
best available answer is the publication time the provider gave us — which is
exactly what it means, and is recorded as such rather than guessed at.

`article_id` is recovered from `external_id`, which has always been the provider
prefix plus the bare id.
"""
from django.db import migrations
from django.db.models import F


def forwards(apps, schema_editor):
    NewsItem = apps.get_model('main_app', 'NewsItem')
    NewsItem.objects.filter(first_public_at__isnull=True).update(first_public_at=F('published_at'))
    for item in NewsItem.objects.exclude(external_id='').only('id', 'external_id', 'article_id'):
        bare = item.external_id.split(':', 1)[-1]
        if bare and item.article_id != bare:
            NewsItem.objects.filter(pk=item.pk).update(article_id=bare[:64])


def backwards(apps, schema_editor):
    apps.get_model('main_app', 'NewsItem').objects.update(first_public_at=None, article_id='')


class Migration(migrations.Migration):
    dependencies = [('main_app', '0021_phase1_clocks')]
    operations = [migrations.RunPython(forwards, backwards)]

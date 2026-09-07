from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('main_app', '0008_forex_15min'),
    ]

    operations = [
        migrations.AddField(
            model_name='strategy',
            name='qualification',
            field=models.CharField(
                choices=[('unproven', 'Unproven'), ('qualified', 'Qualified'), ('quarantine', 'Quarantined')],
                default='unproven',
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name='strategy',
            name='qualification_reason',
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name='strategy',
            name='qualification_updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

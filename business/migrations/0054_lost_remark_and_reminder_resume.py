from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0053_merge_20260922_1653"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientsubscription",
            name="lost_remark",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="clientsubscription",
            name="lost_reminder_resume_on",
            field=models.DateField(
                blank=True,
                help_text="After Lost, client renewal reminders start again on this date.",
                null=True,
            ),
        ),
    ]

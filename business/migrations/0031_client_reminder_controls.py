from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("business", "0030_deal_status_cancel"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="use_custom_reminder",
            field=models.BooleanField(
                default=False,
                help_text="When on, this client uses its own reminder schedule and/or email wording.",
                verbose_name="use custom reminder",
            ),
        ),
        migrations.AddField(
            model_name="client",
            name="reminder_schedule",
            field=models.JSONField(
                blank=True,
                help_text="Custom reminder phases for this client. Blank fields inherit company default.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="client",
            name="reminder_email_subject",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="client",
            name="reminder_email_initial_paragraph",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="client",
            name="reminder_email_end_paragraph",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="clientsubscription",
            name="expiry_reminder_stopped_for_end_date",
            field=models.DateField(
                blank=True,
                help_text="When set to the current end date, client reminder emails stop "
                "until the subscription is renewed with a new end date.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="subscriptionexpirydispatch",
            name="threshold_window",
            field=models.IntegerField(),
        ),
    ]

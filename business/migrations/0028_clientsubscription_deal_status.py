from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0027_client_renewal_emails_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientsubscription",
            name="deal_status",
            field=models.CharField(
                blank=True,
                choices=[("win", "Win"), ("lost", "Lost")],
                default="",
                max_length=16,
            ),
        ),
    ]

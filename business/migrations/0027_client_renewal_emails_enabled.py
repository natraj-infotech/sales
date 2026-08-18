from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("business", "0026_client_extra_emails_contacts"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="renewal_emails_enabled",
            field=models.BooleanField(
                default=True,
                help_text="When off, this client is not sent expiry or renewal reminder emails.",
                verbose_name="renewal emails",
            ),
        ),
    ]

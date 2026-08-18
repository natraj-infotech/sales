from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0031_client_reminder_controls"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscriptionrenewalinvite",
            name="submitted_by_email",
            field=models.EmailField(blank=True, max_length=254),
        ),
    ]

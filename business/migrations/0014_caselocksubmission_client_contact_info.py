from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0013_caselocksubmission_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="caselocksubmission",
            name="client_contact_info",
            field=models.CharField(blank=True, max_length=255, verbose_name="client contact info"),
        ),
    ]

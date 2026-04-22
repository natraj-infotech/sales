from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0014_caselocksubmission_client_contact_info"),
    ]

    operations = [
        migrations.AddField(
            model_name="caselocksubmission",
            name="client_email",
            field=models.EmailField(blank=True, max_length=254, verbose_name="client email 1"),
        ),
        migrations.AddField(
            model_name="caselocksubmission",
            name="client_email_2",
            field=models.EmailField(blank=True, max_length=254, verbose_name="client email 2"),
        ),
        migrations.AddField(
            model_name="caselocksubmission",
            name="client_phone",
            field=models.CharField(blank=True, max_length=64, verbose_name="client phone"),
        ),
    ]

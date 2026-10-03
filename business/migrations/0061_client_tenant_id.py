from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0060_client_pan_number"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="tenant_id",
            field=models.CharField(blank=True, max_length=255, verbose_name="Tenant ID"),
        ),
    ]

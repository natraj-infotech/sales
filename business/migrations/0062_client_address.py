from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0061_client_tenant_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="address",
            field=models.TextField(blank=True),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0059_product_vendors"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="pan_number",
            field=models.CharField(blank=True, max_length=10, verbose_name="PAN number"),
        ),
    ]

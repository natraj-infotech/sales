from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("business", "0015_caselocksubmission_client_contact_channels"),
    ]

    operations = [
        migrations.AlterField(
            model_name="clientsubscription",
            name="product",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="subscriptions",
                to="business.product",
            ),
        ),
        migrations.AlterField(
            model_name="clientsubscription",
            name="vendor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="business.vendor",
            ),
        ),
    ]

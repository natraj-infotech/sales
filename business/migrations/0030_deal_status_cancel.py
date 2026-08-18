from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0029_renewal_invite_deal_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="clientsubscription",
            name="deal_status",
            field=models.CharField(
                blank=True,
                choices=[("win", "Win"), ("lost", "Lost"), ("cancel", "Cancel")],
                default="",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="subscriptionrenewalinvite",
            name="deal_status",
            field=models.CharField(
                blank=True,
                choices=[("win", "Win"), ("lost", "Lost"), ("cancel", "Cancel")],
                default="",
                max_length=16,
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0028_clientsubscription_deal_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscriptionrenewalinvite",
            name="deal_status",
            field=models.CharField(
                blank=True,
                choices=[("win", "Win"), ("lost", "Lost")],
                default="",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="subscriptionrenewalinvite",
            name="deal_status_set_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

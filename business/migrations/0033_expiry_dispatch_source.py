from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0032_renewal_invite_submitted_by_email"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="subscriptionexpirydispatch",
            name="unique_expiry_dispatch_per_send_slot",
        ),
        migrations.AddField(
            model_name="subscriptionexpirydispatch",
            name="source",
            field=models.CharField(
                choices=[("auto", "Automatic"), ("manual", "Manual")],
                default="auto",
                max_length=10,
            ),
        ),
        migrations.AddConstraint(
            model_name="subscriptionexpirydispatch",
            constraint=models.UniqueConstraint(
                fields=("subscription", "threshold_window", "sent_on", "send_number", "source"),
                name="unique_expiry_dispatch_per_send_slot",
            ),
        ),
    ]

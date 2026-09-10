import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0039_caselocksubmission_status_changed_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientsubscription",
            name="renewal_draft_billing_amount",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=12, null=True
            ),
        ),
        migrations.AddField(
            model_name="clientsubscription",
            name="renewal_draft_end_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="clientsubscription",
            name="renewal_draft_quantity",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="clientsubscription",
            name="renewal_draft_start_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="clientsubscription",
            name="renewal_draft_vendor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="renewal_draft_subscriptions",
                to="business.vendor",
            ),
        ),
        migrations.AddField(
            model_name="clientsubscription",
            name="win_form_completed",
            field=models.BooleanField(default=False),
        ),
    ]

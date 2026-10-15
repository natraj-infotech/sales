from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0065_ticket_email_conversation"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientsubscription",
            name="zoho_invoice_id",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="clientsubscription",
            name="zoho_line_item_id",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddConstraint(
            model_name="clientsubscription",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    models.Q(("zoho_invoice_id", ""), _negated=True),
                    models.Q(("zoho_line_item_id", ""), _negated=True),
                ),
                fields=("company", "zoho_invoice_id", "zoho_line_item_id"),
                name="unique_company_zoho_invoice_line",
            ),
        ),
    ]

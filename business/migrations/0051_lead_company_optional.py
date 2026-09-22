# Generated manually for optional Lead.company

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0017_procurement_module"),
        ("business", "0050_merge_lead_whatsapp_and_renewal_draft"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="lead",
            name="unique_lead_whatsapp_message_per_company",
        ),
        migrations.RemoveConstraint(
            model_name="lead",
            name="unique_lead_email_message_per_company",
        ),
        migrations.AlterField(
            model_name="lead",
            name="company",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="leads",
                to="accounts.companyprofile",
            ),
        ),
        migrations.AddConstraint(
            model_name="lead",
            constraint=models.UniqueConstraint(
                condition=models.Q(("whatsapp_message_id", ""), _negated=True)
                & models.Q(("company__isnull", False)),
                fields=("company", "whatsapp_message_id"),
                name="unique_lead_whatsapp_message_per_company",
            ),
        ),
        migrations.AddConstraint(
            model_name="lead",
            constraint=models.UniqueConstraint(
                condition=models.Q(("whatsapp_message_id", ""), _negated=True)
                & models.Q(("company__isnull", True)),
                fields=("whatsapp_message_id",),
                name="unique_lead_whatsapp_message_unassigned",
            ),
        ),
        migrations.AddConstraint(
            model_name="lead",
            constraint=models.UniqueConstraint(
                condition=models.Q(("email_message_id", ""), _negated=True)
                & models.Q(("company__isnull", False)),
                fields=("company", "email_message_id"),
                name="unique_lead_email_message_per_company",
            ),
        ),
        migrations.AddConstraint(
            model_name="lead",
            constraint=models.UniqueConstraint(
                condition=models.Q(("email_message_id", ""), _negated=True)
                & models.Q(("company__isnull", True)),
                fields=("email_message_id",),
                name="unique_lead_email_message_unassigned",
            ),
        ),
    ]

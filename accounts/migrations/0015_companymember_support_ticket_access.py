from django.db import migrations, models


def grant_owners_support_ticket_access(apps, schema_editor):
    CompanyMember = apps.get_model("accounts", "CompanyMember")
    CompanyMember.objects.filter(role="owner").update(support_ticket_access=True)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0014_company_caselock_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="companymember",
            name="support_ticket_access",
            field=models.BooleanField(
                default=False,
                help_text="Can view and manage the support tickets dashboard for this company.",
            ),
        ),
        migrations.RunPython(
            grant_owners_support_ticket_access,
            migrations.RunPython.noop,
        ),
    ]

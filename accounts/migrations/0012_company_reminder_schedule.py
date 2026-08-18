from django.db import migrations, models


def fill_existing_company_schedules(apps, schema_editor):
    CompanyProfile = apps.get_model("accounts", "CompanyProfile")
    from business.reminder_schedule import light_schedule, parse_day_list

    for company in CompanyProfile.objects.all():
        days = parse_day_list(company.expiry_notify_at_days) or [30, 15, 7]
        company.expiry_reminder_preset = "light"
        company.expiry_reminder_schedule = light_schedule(days)
        company.save(
            update_fields=["expiry_reminder_preset", "expiry_reminder_schedule"]
        )


def noop_reverse(apps, schema_editor):
    return None


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0011_company_expiry_email_paragraphs"),
    ]

    operations = [
        migrations.AddField(
            model_name="companyprofile",
            name="expiry_email_subject",
            field=models.CharField(
                blank=True,
                help_text="Subject for client renewal reminder emails. "
                "Leave blank to use the default. Merge tags: "
                "{{client_name}}, {{product_name}}, {{days_left}}, {{expiry_date}}.",
                max_length=255,
                verbose_name="expiry email subject",
            ),
        ),
        migrations.AddField(
            model_name="companyprofile",
            name="expiry_reminder_preset",
            field=models.CharField(
                default="recommended",
                help_text="light, recommended, or custom reminder cadence.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="companyprofile",
            name="expiry_reminder_schedule",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="List of reminder phases (from/to days, send mode, times).",
            ),
        ),
        migrations.RunPython(fill_existing_company_schedules, noop_reverse),
    ]

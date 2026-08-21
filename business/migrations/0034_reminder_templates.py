import json

from django.db import migrations, models
import django.db.models.deletion


def migrate_company_schedules_to_templates(apps, schema_editor):
    CompanyProfile = apps.get_model("accounts", "CompanyProfile")
    Client = apps.get_model("business", "Client")
    ReminderTemplate = apps.get_model("business", "ReminderTemplate")

    for company in CompanyProfile.objects.all().iterator():
        schedule = list(company.expiry_reminder_schedule or [])
        if not schedule:
            preset = (company.expiry_reminder_preset or "recommended").strip()
            if preset == "light":
                days_raw = (company.expiry_notify_at_days or "30,15,7").strip()
                days = []
                for part in days_raw.replace(";", ",").split(","):
                    part = part.strip()
                    if not part:
                        continue
                    try:
                        value = int(part)
                    except ValueError:
                        continue
                    if value > 0 and value not in days:
                        days.append(value)
                days.sort(reverse=True)
                if not days:
                    days = [30, 15, 7]
                schedule = [
                    {
                        "name": f"{day} days before",
                        "from_days": day,
                        "to_days": day,
                        "mode": "total_in_window",
                        "count": 1,
                        "times": ["09:00"],
                    }
                    for day in days
                ]
            else:
                schedule = [
                    {
                        "name": "Early window",
                        "from_days": 30,
                        "to_days": 16,
                        "mode": "weekly",
                        "count": 1,
                        "times": ["09:00"],
                    },
                    {
                        "name": "Mid window",
                        "from_days": 15,
                        "to_days": 7,
                        "mode": "alternate",
                        "count": 1,
                        "times": ["09:00"],
                    },
                    {
                        "name": "Daily push",
                        "from_days": 6,
                        "to_days": 4,
                        "mode": "daily",
                        "count": 1,
                        "times": ["09:00"],
                    },
                    {
                        "name": "Last 3 days",
                        "from_days": 3,
                        "to_days": 1,
                        "mode": "times_per_day",
                        "count": 3,
                        "times": ["09:00", "14:00", "18:00"],
                    },
                    {
                        "name": "Expiry day",
                        "from_days": 0,
                        "to_days": 0,
                        "mode": "times_per_day",
                        "count": 3,
                        "times": ["09:00", "14:00", "18:00"],
                    },
                    {
                        "name": "Overdue",
                        "from_days": -1,
                        "to_days": -7,
                        "mode": "daily",
                        "count": 1,
                        "times": ["09:00"],
                    },
                ]

        default_template = ReminderTemplate.objects.create(
            company_id=company.pk,
            name="Standard Renewal",
            schedule=schedule,
            is_default=True,
        )

        schedule_cache: dict[str, int] = {
            json.dumps(schedule, sort_keys=True): default_template.pk
        }

        for client in Client.objects.filter(company_id=company.pk).iterator():
            if not getattr(client, "use_custom_reminder", False):
                continue
            client_schedule = client.reminder_schedule
            if not client_schedule:
                continue
            cache_key = json.dumps(client_schedule, sort_keys=True)
            template_id = schedule_cache.get(cache_key)
            if template_id is None:
                template = ReminderTemplate.objects.create(
                    company_id=company.pk,
                    name=f"{client.name} Reminder",
                    schedule=client_schedule,
                    is_default=False,
                )
                template_id = template.pk
                schedule_cache[cache_key] = template_id
            client.reminder_template_id = template_id
            client.save(update_fields=["reminder_template_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0012_company_reminder_schedule"),
        ("business", "0033_expiry_dispatch_source"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReminderTemplate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=120)),
                (
                    "schedule",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="List of reminder phases (from/to days, send mode, times).",
                    ),
                ),
                (
                    "is_default",
                    models.BooleanField(
                        default=False,
                        help_text="Clients with no template assigned use the company default template.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reminder_templates",
                        to="accounts.companyprofile",
                    ),
                ),
            ],
            options={
                "ordering": ["-is_default", "name"],
            },
        ),
        migrations.AddConstraint(
            model_name="remindertemplate",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_default", True)),
                fields=("company",),
                name="unique_default_reminder_template_per_company",
            ),
        ),
        migrations.AddField(
            model_name="client",
            name="reminder_template",
            field=models.ForeignKey(
                blank=True,
                help_text="When blank, the company default reminder template is used.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="clients",
                to="business.remindertemplate",
            ),
        ),
        migrations.RunPython(
            migrate_company_schedules_to_templates,
            migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name="client",
            name="reminder_email_end_paragraph",
        ),
        migrations.RemoveField(
            model_name="client",
            name="reminder_email_initial_paragraph",
        ),
        migrations.RemoveField(
            model_name="client",
            name="reminder_email_subject",
        ),
        migrations.RemoveField(
            model_name="client",
            name="reminder_schedule",
        ),
        migrations.RemoveField(
            model_name="client",
            name="use_custom_reminder",
        ),
    ]

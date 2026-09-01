from django.db import migrations, models
from django.utils import timezone


def backfill_final_status_changed_at(apps, schema_editor):
    CaseLockSubmission = apps.get_model("business", "CaseLockSubmission")
    now = timezone.now()
    CaseLockSubmission.objects.filter(
        status="approved",
        status_changed_at__isnull=True,
    ).update(status_changed_at=now)


class Migration(migrations.Migration):
    dependencies = [
        ("business", "0038_caselock_company_products"),
    ]

    operations = [
        migrations.AddField(
            model_name="caselocksubmission",
            name="status_changed_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When Final or Decline was set.",
                null=True,
            ),
        ),
        migrations.RunPython(backfill_final_status_changed_at, migrations.RunPython.noop),
    ]

from django.db import migrations


def repair_locked_default_cycles(apps, schema_editor):
    from business.reminder_schedule import (
        DEFAULT_REMINDER_CYCLE_NAME,
        default_reminder_cycle_schedule,
    )

    ReminderTemplate = apps.get_model("business", "ReminderTemplate")
    fixed_schedule = default_reminder_cycle_schedule()
    for template in ReminderTemplate.objects.filter(is_default=True).iterator():
        template.name = DEFAULT_REMINDER_CYCLE_NAME
        template.schedule = fixed_schedule
        template.save(update_fields=["name", "schedule", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0034_reminder_templates"),
    ]

    operations = [
        migrations.RunPython(repair_locked_default_cycles, migrations.RunPython.noop),
    ]

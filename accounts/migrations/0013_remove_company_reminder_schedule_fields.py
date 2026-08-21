from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0012_company_reminder_schedule"),
        ("business", "0034_reminder_templates"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="companyprofile",
            name="expiry_reminder_preset",
        ),
        migrations.RemoveField(
            model_name="companyprofile",
            name="expiry_reminder_schedule",
        ),
    ]

from django.db import migrations


def create_owner_memberships(apps, schema_editor):
    CompanyProfile = apps.get_model("accounts", "CompanyProfile")
    CompanyMember = apps.get_model("accounts", "CompanyMember")

    for profile in CompanyProfile.objects.all():
        CompanyMember.objects.update_or_create(
            company=profile,
            user=profile.user,
            defaults={
                "role": "owner",
                "sales_access": True,
                "can_grant_sales": True,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_companymember"),
    ]

    operations = [
        migrations.RunPython(create_owner_memberships, migrations.RunPython.noop),
    ]

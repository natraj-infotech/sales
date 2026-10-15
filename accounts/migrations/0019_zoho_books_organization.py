from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0018_company_support_digest_last_sent"),
    ]

    operations = [
        migrations.AddField(
            model_name="companyprofile",
            name="zoho_books_organization_id",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]

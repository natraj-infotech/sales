from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0019_zoho_books_organization"),
    ]

    operations = [
        migrations.AddField(
            model_name="companyprofile",
            name="zoho_books_organization_name",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]

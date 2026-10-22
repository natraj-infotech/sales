from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0020_zoho_books_organization_name"),
        ("business", "0068_merge_20261015_1451"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EngineerPendingDigest",
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
                ("sent_on", models.DateField()),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="engineer_pending_digests",
                        to="accounts.companyprofile",
                    ),
                ),
                (
                    "engineer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="engineer_pending_digests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "engineer pending digest",
                "verbose_name_plural": "engineer pending digests",
            },
        ),
        migrations.AddConstraint(
            model_name="engineerpendingdigest",
            constraint=models.UniqueConstraint(
                fields=("company", "engineer", "sent_on"),
                name="uniq_engineer_pending_digest_day",
            ),
        ),
    ]

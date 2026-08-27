import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0036_renewal_invite_new_products"),
    ]

    operations = [
        migrations.CreateModel(
            name="SubscriptionRenewalInviteRecipient",
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
                ("email", models.EmailField(max_length=254)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("opened_at", models.DateTimeField(blank=True, null=True)),
                ("internet_message_id", models.CharField(blank=True, max_length=998)),
                (
                    "invite",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="recipients",
                        to="business.subscriptionrenewalinvite",
                    ),
                ),
            ],
            options={
                "ordering": ["id"],
            },
        ),
        migrations.AddConstraint(
            model_name="subscriptionrenewalinviterecipient",
            constraint=models.UniqueConstraint(
                fields=("invite", "email"),
                name="uniq_renewal_invite_recipient_email",
            ),
        ),
    ]

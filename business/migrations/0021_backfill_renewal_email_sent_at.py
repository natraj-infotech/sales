from django.db import migrations
from django.db.models import F


def backfill_email_sent_at(apps, schema_editor):
    Invite = apps.get_model("business", "SubscriptionRenewalInvite")
    Invite.objects.filter(email_sent_at__isnull=True).update(email_sent_at=F("created_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0020_renewal_invite_tracking"),
    ]

    operations = [
        migrations.RunPython(backfill_email_sent_at, migrations.RunPython.noop),
    ]

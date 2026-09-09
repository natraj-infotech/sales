from django.db import migrations, models
from django.db.models import Count
from django.db.models.functions import Lower, Trim


SUBSCRIPTION_VALUE_FIELDS = (
    "client_id",
    "product_id",
    "vendor_id",
    "status",
    "deal_status",
    "start_date",
    "end_date",
    "quantity",
    "billing_amount",
    "invoice_number",
    "serial_number",
    "notes",
    "expiry_reminder_stopped_for_end_date",
)


def _move_expiry_notices(ExpiryNotice, canonical_id, duplicate_id):
    for notice in ExpiryNotice.objects.filter(subscription_id=duplicate_id).iterator():
        conflict = ExpiryNotice.objects.filter(
            subscription_id=canonical_id,
            days_before=notice.days_before,
        ).first()
        if conflict:
            # One row is enough to record that this reminder was sent. Keep the
            # most recent timestamp where both duplicate subscriptions have one.
            if notice.sent_at > conflict.sent_at:
                conflict.delete()
                notice.subscription_id = canonical_id
                notice.save(update_fields=["subscription"])
            else:
                notice.delete()
        else:
            notice.subscription_id = canonical_id
            notice.save(update_fields=["subscription"])


def _move_expiry_dispatches(ExpiryDispatch, canonical_id, duplicate_id):
    for dispatch in ExpiryDispatch.objects.filter(
        subscription_id=duplicate_id
    ).iterator():
        conflict = ExpiryDispatch.objects.filter(
            subscription_id=canonical_id,
            threshold_window=dispatch.threshold_window,
            sent_on=dispatch.sent_on,
            send_number=dispatch.send_number,
            source=dispatch.source,
        ).first()
        if conflict:
            # Preserve the latest record for an already-used delivery slot.
            if dispatch.sent_at > conflict.sent_at:
                conflict.delete()
                dispatch.subscription_id = canonical_id
                dispatch.save(update_fields=["subscription"])
            else:
                dispatch.delete()
        else:
            dispatch.subscription_id = canonical_id
            dispatch.save(update_fields=["subscription"])


def merge_normalized_subscription_duplicates(apps, schema_editor):
    ClientSubscription = apps.get_model("business", "ClientSubscription")
    ExpiryNotice = apps.get_model("business", "SubscriptionExpiryNotice")
    InviteLine = apps.get_model("business", "SubscriptionRenewalInviteLine")
    ExpiryDispatch = apps.get_model("business", "SubscriptionExpiryDispatch")

    duplicate_groups = (
        ClientSubscription.objects.annotate(
            normalized_code=Lower(Trim("subscription_code"))
        )
        .filter(subscription_code__regex=r"\S")
        .values("company_id", "normalized_code")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
    )

    for group in duplicate_groups.iterator():
        subscriptions = list(
            ClientSubscription.objects.annotate(
                normalized_code=Lower(Trim("subscription_code"))
            )
            .filter(
                company_id=group["company_id"],
                normalized_code=group["normalized_code"],
            )
            .order_by("id")
        )
        if len(subscriptions) < 2:
            continue

        # Keep the oldest primary key so existing links remain stable, but copy
        # the latest imported subscription values onto it.
        canonical = subscriptions[0]
        latest = max(
            subscriptions,
            key=lambda subscription: (
                subscription.created_at,
                subscription.pk,
            ),
        )
        for field_name in SUBSCRIPTION_VALUE_FIELDS:
            setattr(canonical, field_name, getattr(latest, field_name))
        canonical.subscription_code = latest.subscription_code.strip()
        canonical.save(
            update_fields=[*SUBSCRIPTION_VALUE_FIELDS, "subscription_code"]
        )

        for duplicate in subscriptions[1:]:
            _move_expiry_notices(ExpiryNotice, canonical.pk, duplicate.pk)
            _move_expiry_dispatches(ExpiryDispatch, canonical.pk, duplicate.pk)
            InviteLine.objects.filter(subscription_id=duplicate.pk).update(
                subscription_id=canonical.pk
            )
            duplicate.delete()


class Migration(migrations.Migration):
    # PostgreSQL cannot create the unique index while the preceding data
    # cleanup has pending foreign-key trigger events in the same transaction.
    atomic = False

    dependencies = [
        ("business", "0039_caselocksubmission_status_changed_at"),
    ]

    operations = [
        migrations.RunPython(
            merge_normalized_subscription_duplicates,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="clientsubscription",
            constraint=models.UniqueConstraint(
                models.F("company"),
                Lower(Trim("subscription_code")),
                condition=models.Q(subscription_code__regex=r"\S"),
                name="unique_company_normalized_subscription_code",
            ),
        ),
    ]

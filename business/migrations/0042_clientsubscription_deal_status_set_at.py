from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0041_merge_0040_leaves"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientsubscription",
            name="deal_status_set_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

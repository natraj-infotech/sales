from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0012_caselocksubmission_timestamp"),
    ]

    operations = [
        migrations.AddField(
            model_name="caselocksubmission",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("approved", "Approved"),
                    ("declined", "Declined"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
    ]

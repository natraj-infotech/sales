from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0065_ticket_email_conversation"),
    ]

    operations = [
        migrations.AddField(
            model_name="supportticketmeta",
            name="source_channel",
            field=models.CharField(
                blank=True,
                choices=[
                    ("email", "Email"),
                    ("whatsapp", "WhatsApp"),
                    ("internal", "Internal"),
                ],
                default="",
                help_text="How the customer opened the ticket (email, WhatsApp, etc.).",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="supportticketmeta",
            name="customer_phone",
            field=models.CharField(
                blank=True,
                help_text="Customer mobile for WhatsApp-originated tickets.",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="supportticketmeta",
            name="customer_firm_name",
            field=models.CharField(
                blank=True,
                help_text="Customer or firm name from WhatsApp support intake.",
                max_length=255,
            ),
        ),
    ]

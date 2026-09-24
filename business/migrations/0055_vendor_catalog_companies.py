from django.db import migrations, models


def copy_product_companies(apps, schema_editor):
    Vendor = apps.get_model("business", "Vendor")
    for vendor in Vendor.objects.all().iterator():
        names = []
        seen = set()
        for product in vendor.catalog_products.all():
            label = (product.company_name or "").strip()
            key = label.lower()
            if label and key not in seen:
                seen.add(key)
                names.append(label)
        vendor.catalog_companies = names
        vendor.save(update_fields=["catalog_companies"])


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0054_lost_remark_and_reminder_resume"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendor",
            name="catalog_companies",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Product companies this vendor can quote in procurement.",
            ),
        ),
        migrations.RunPython(copy_product_companies, migrations.RunPython.noop),
    ]

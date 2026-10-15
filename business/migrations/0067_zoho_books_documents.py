from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0020_zoho_books_organization_name"),
        ("business", "0066_zoho_books_invoice_line"),
    ]

    operations = [
        migrations.CreateModel(
            name="ZohoBooksSync",
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
                (
                    "organization_id",
                    models.CharField(max_length=64),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("invoice", "Sales invoice"),
                            ("bill", "Purchase bill"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "mode",
                    models.CharField(
                        choices=[
                            ("idle", "Idle"),
                            ("full", "Full"),
                            ("incremental", "Incremental"),
                        ],
                        default="idle",
                        max_length=16,
                    ),
                ),
                ("next_page", models.PositiveIntegerField(default=1)),
                ("last_completed_at", models.DateTimeField(blank=True, null=True)),
                ("modified_after", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="zoho_books_syncs",
                        to="accounts.companyprofile",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ZohoBooksDocument",
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
                ("organization_id", models.CharField(max_length=64)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("invoice", "Sales invoice"),
                            ("bill", "Purchase bill"),
                        ],
                        max_length=16,
                    ),
                ),
                ("zoho_id", models.CharField(max_length=64)),
                ("number", models.CharField(blank=True, max_length=128)),
                ("document_date", models.DateField(blank=True, null=True)),
                ("due_date", models.DateField(blank=True, null=True)),
                ("party_name", models.CharField(blank=True, max_length=255)),
                ("gst_no", models.CharField(blank=True, max_length=32)),
                ("status", models.CharField(blank=True, max_length=32)),
                ("currency_code", models.CharField(blank=True, max_length=8)),
                (
                    "total",
                    models.DecimalField(decimal_places=2, default=0, max_digits=14),
                ),
                (
                    "balance",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=14, null=True
                    ),
                ),
                ("last_modified_raw", models.CharField(blank=True, max_length=40)),
                ("synced_at", models.DateTimeField(auto_now=True)),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="zoho_books_documents",
                        to="accounts.companyprofile",
                    ),
                ),
            ],
            options={
                "ordering": ["-document_date", "-id"],
            },
        ),
        migrations.CreateModel(
            name="ZohoBooksDocumentLine",
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
                ("line_id", models.CharField(max_length=64)),
                ("item_id", models.CharField(blank=True, max_length=64)),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
                ("sku", models.CharField(blank=True, max_length=128)),
                ("quantity", models.PositiveIntegerField(default=1)),
                (
                    "amount",
                    models.DecimalField(decimal_places=2, default=0, max_digits=14),
                ),
                ("serial_number", models.CharField(blank=True, max_length=128)),
                ("sort_order", models.PositiveIntegerField(default=0)),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="lines",
                        to="business.zohobooksdocument",
                    ),
                ),
            ],
            options={
                "ordering": ["sort_order", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="zohobookssync",
            constraint=models.UniqueConstraint(
                fields=("company", "organization_id", "kind"),
                name="unique_zoho_books_sync",
            ),
        ),
        migrations.AddConstraint(
            model_name="zohobooksdocument",
            constraint=models.UniqueConstraint(
                fields=("company", "organization_id", "kind", "zoho_id"),
                name="unique_zoho_books_document",
            ),
        ),
        migrations.AddIndex(
            model_name="zohobooksdocument",
            index=models.Index(
                fields=["company", "organization_id", "kind", "document_date"],
                name="zoho_doc_period_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="zohobooksdocument",
            index=models.Index(
                fields=["company", "organization_id", "kind", "number"],
                name="zoho_doc_number_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="zohobooksdocumentline",
            constraint=models.UniqueConstraint(
                fields=("document", "line_id"),
                name="unique_zoho_books_document_line",
            ),
        ),
        migrations.AddIndex(
            model_name="zohobooksdocumentline",
            index=models.Index(fields=["item_id"], name="zoho_line_item_idx"),
        ),
    ]

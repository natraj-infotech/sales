"""Public lead enquiry form (WhatsApp CTA destination)."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import CompanyProfile
from business.lead_public_tokens import sign_lead_public_token, unsign_lead_public_token
from business.lead_public_urls import build_lead_public_enquiry_url
from business.models import Lead, Product

User = get_user_model()


class LeadPublicTokenTests(TestCase):
    def test_sign_unsign(self):
        token = sign_lead_public_token(
            company_id=42,
            whatsapp_wa_id="919876543210",
            contact_name="Asha",
        )
        parsed = unsign_lead_public_token(token)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["company_id"], 42)
        self.assertEqual(parsed["whatsapp_wa_id"], "919876543210")
        self.assertEqual(parsed["contact_name"], "Asha")

    def test_invalid_token(self):
        self.assertIsNone(unsign_lead_public_token("not-valid"))


@override_settings(PUBLIC_BASE_URL="https://example.test")
class LeadPublicFormViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        user = User.objects.create_user(email="owner@example.com", password="x")
        self.company = CompanyProfile.objects.create(user=user, company_name="Acme")
        self.product = Product.objects.create(company=self.company, name="Suite")

    def _url(self, **token_kwargs):
        token = sign_lead_public_token(
            company_id=self.company.pk,
            whatsapp_wa_id="919876543210",
            contact_name="Asha",
            **token_kwargs,
        )
        return reverse("lead_public_enquiry", kwargs={"token": token})

    def test_get_prefills_name_and_phone(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Asha")
        self.assertContains(resp, "919876543210")

    def test_post_creates_whatsapp_lead(self):
        url = self._url()
        resp = self.client.post(
            url,
            {
                "contact_name": "Asha",
                "company_name": "Buyer Ltd",
                "email": "asha@buyer.com",
                "phone": "919876543210",
                "requirement": "Need demo",
                "products": [self.product.pk],
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Thank you")
        lead = Lead.objects.get(company=self.company, contact_name="Asha")
        self.assertEqual(lead.source, Lead.Source.WHATSAPP)
        self.assertEqual(lead.whatsapp_wa_id, "919876543210")
        self.assertEqual(lead.requirement, "Need demo")
        self.assertEqual(lead.products.get(), self.product)

    def test_build_absolute_url(self):
        url = build_lead_public_enquiry_url(
            company=self.company,
            whatsapp_wa_id="911",
            contact_name="X",
        )
        self.assertTrue(url.startswith("https://example.test/leads/enquiry/"))

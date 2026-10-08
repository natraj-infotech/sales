from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from helpdesk.models import Ticket

from accounts.models import CompanyMember, CompanyProfile
from business import helpdesk_bridge
from business.models import SupportTeam
from business.support_team import (
    assignable_users,
    auto_assign_ticket,
    save_team,
)


@patch("business.helpdesk_assign_mail.queue_assignment_emails")
@patch("business.helpdesk_assign_mail.queue_ticket_created_email")
class SupportTeamAutoAssignTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("owner@team.example", "pass")
        self.alice = User.objects.create_user("alice@team.example", "pass")
        self.bob = User.objects.create_user("bob@team.example", "pass")
        self.outsider = User.objects.create_user("outsider@else.example", "pass")
        self.company = CompanyProfile.objects.create(
            user=self.owner, company_name="Team Co"
        )
        for user in (self.alice, self.bob):
            CompanyMember.objects.create(
                company=self.company, user=user, support_ticket_access=True
            )

    def _internal_ticket(self, subject="Help"):
        return helpdesk_bridge.create_internal_ticket(
            company=self.company,
            user=self.owner,
            subject=subject,
            description="Details",
        )

    def _enable(self, *users):
        save_team(
            self.company,
            member_ids=[u.pk for u in users],
            auto_assign_enabled=True,
        )

    def test_auto_assign_off_leaves_ticket_unassigned(self, *_mocks):
        save_team(
            self.company,
            member_ids=[self.alice.pk],
            auto_assign_enabled=False,
        )
        ticket = self._internal_ticket()
        ticket.refresh_from_db()
        self.assertIsNone(ticket.assigned_to_id)

    def test_new_ticket_goes_to_engineer_with_fewest_open_tickets(self, *_mocks):
        self._enable(self.alice, self.bob)
        busy = self._internal_ticket("Busy")
        busy.refresh_from_db()
        first = busy.assigned_to_id

        second = self._internal_ticket("Next")
        second.refresh_from_db()
        self.assertNotEqual(second.assigned_to_id, first)
        self.assertIn(second.assigned_to_id, {self.alice.pk, self.bob.pk})

    def test_ties_rotate_between_engineers(self, *_mocks):
        self._enable(self.alice, self.bob)
        assigned = []
        for i in range(4):
            t = self._internal_ticket(f"T{i}")
            Ticket.objects.filter(pk=t.pk).update(status=Ticket.CLOSED_STATUS)
            t.refresh_from_db()
            assigned.append(t.assigned_to_id)
        self.assertEqual(
            assigned, [self.alice.pk, self.bob.pk, self.alice.pk, self.bob.pk]
        )

    def test_closed_tickets_do_not_count_as_load(self, *_mocks):
        self._enable(self.alice, self.bob)
        queue = helpdesk_bridge.get_company_queue(self.company)
        for _ in range(3):
            Ticket.objects.create(
                title="Old",
                queue=queue,
                assigned_to=self.alice,
                status=Ticket.CLOSED_STATUS,
            )
        Ticket.objects.create(
            title="Open", queue=queue, assigned_to=self.bob, status=Ticket.OPEN_STATUS
        )
        ticket = self._internal_ticket()
        ticket.refresh_from_db()
        self.assertEqual(ticket.assigned_to_id, self.alice.pk)

    def test_assignment_is_logged_and_engineer_notified(self, queued_created, queued_assign):
        self._enable(self.alice)
        with patch(
            "business.team_notifications.notify_ticket_assigned"
        ) as notified:
            ticket = self._internal_ticket()
        ticket.refresh_from_db()
        self.assertEqual(ticket.assigned_to_id, self.alice.pk)
        notified.assert_called_once()
        queued_assign.assert_called_once()
        self.assertEqual(queued_assign.call_args.kwargs["assignee"], self.alice)
        self.assertTrue(
            ticket.followup_set.filter(comment__icontains="Auto-assigned").exists()
        )

    def test_empty_team_leaves_ticket_unassigned(self, *_mocks):
        self._enable()
        ticket = self._internal_ticket()
        ticket.refresh_from_db()
        self.assertIsNone(ticket.assigned_to_id)

    def test_oem_ticket_is_not_auto_assigned(self, *_mocks):
        self._enable(self.alice)
        ticket = helpdesk_bridge.create_oem_ticket(
            company=self.company,
            user=self.owner,
            subject="Vendor issue",
            description="Escalate",
        )
        self.assertIsNone(auto_assign_ticket(self.company, ticket))
        ticket.refresh_from_db()
        self.assertIsNone(ticket.assigned_to_id)

    def test_email_ticket_is_auto_assigned(self, queued_created, _queued_assign):
        from business.helpdesk_inbox import create_helpdesk_ticket_from_email

        self._enable(self.bob)
        ticket = create_helpdesk_ticket_from_email(
            company=self.company,
            subject="Printer down",
            body="Please help",
            sender="client@example.com",
            message_id="<abc@example.com>",
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.assigned_to_id, self.bob.pk)
        queued_created.assert_called_once()

    def test_member_who_left_company_is_skipped(self, *_mocks):
        self._enable(self.alice, self.bob)
        CompanyMember.objects.filter(user=self.alice).delete()
        for i in range(2):
            t = self._internal_ticket(f"T{i}")
            t.refresh_from_db()
            self.assertEqual(t.assigned_to_id, self.bob.pk)


class SupportTeamSettingsViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("owner@view.example", "pass")
        self.alice = User.objects.create_user("alice@view.example", "pass")
        self.outsider = User.objects.create_user("outsider@view.example", "pass")
        self.company = CompanyProfile.objects.create(
            user=self.owner, company_name="View Co"
        )
        CompanyMember.objects.create(
            company=self.company, user=self.alice, support_ticket_access=True
        )
        self.client.force_login(self.owner)

    def test_ticket_list_renders_team_modal_with_company_people(self):
        response = self.client.get(reverse("support_ticket_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="st-team-modal"')
        self.assertContains(response, "alice@view.example")
        self.assertNotContains(response, "outsider@view.example")

    def test_old_team_url_opens_list_with_modal(self):
        response = self.client.get(reverse("support_team_settings"))
        self.assertRedirects(
            response,
            f"{reverse('support_ticket_list')}?team=1",
            fetch_redirect_response=False,
        )

    def test_save_members_and_toggle_ignores_outsiders(self):
        response = self.client.post(
            reverse("support_team_settings"),
            {
                "members": [self.alice.pk, self.outsider.pk],
                "auto_assign_enabled": "on",
                "next_qs": "team=1&status=open",
            },
        )
        self.assertRedirects(
            response,
            f"{reverse('support_ticket_list')}?status=open",
            fetch_redirect_response=False,
        )
        team = SupportTeam.objects.get(company=self.company)
        self.assertTrue(team.auto_assign_enabled)
        self.assertEqual(list(team.members.values_list("pk", flat=True)), [self.alice.pk])

    def test_quick_toggle_from_ticket_list(self):
        url = reverse("support_team_auto_assign_toggle")
        self.client.post(url, {"auto_assign_enabled": "on"})
        self.assertTrue(SupportTeam.objects.get(company=self.company).auto_assign_enabled)
        response = self.client.post(url, {})
        self.assertRedirects(
            response, reverse("support_ticket_list"), fetch_redirect_response=False
        )
        self.assertFalse(SupportTeam.objects.get(company=self.company).auto_assign_enabled)

    def test_quick_toggle_from_modal_close_returns_json(self):
        url = reverse("support_team_auto_assign_toggle")
        response = self.client.post(
            url,
            {"auto_assign_enabled": "on"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["enabled"])
        self.assertEqual(data["level"], "warning")
        self.assertIn("support team is empty", data["message"])
        self.assertTrue(SupportTeam.objects.get(company=self.company).auto_assign_enabled)
        save_team(self.company, member_ids=[self.alice.pk], auto_assign_enabled=True)
        response = self.client.post(
            url,
            {},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        data = response.json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["level"], "success")
        self.assertFalse(SupportTeam.objects.get(company=self.company).auto_assign_enabled)

    def test_dropdown_shows_team_plus_current_assignees(self):
        self.assertEqual(
            set(assignable_users(self.company).values_list("pk", flat=True)),
            {self.owner.pk, self.alice.pk},
        )
        save_team(self.company, member_ids=[self.alice.pk], auto_assign_enabled=False)
        self.assertEqual(
            list(assignable_users(self.company).values_list("pk", flat=True)),
            [self.alice.pk],
        )
        self.assertEqual(
            set(
                assignable_users(
                    self.company, include_ids={self.owner.pk}
                ).values_list("pk", flat=True)
            ),
            {self.owner.pk, self.alice.pk},
        )

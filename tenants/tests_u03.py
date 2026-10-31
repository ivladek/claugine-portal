"""u03 (owner 2026-10-31): the project edit form (compact, contacts at the bottom; a stage change alone is saved),
users added or invited (Settings → Platform decides whether adding is allowed), the users' columns, one copy icon."""
from django.core import mail
from django.urls import reverse

from accounts.models import Kind, User
from accounts.tests import Base, valid_bin
from tenants.models import Organization, PortalSettings, Tenant


class U03(Base):
    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True, is_resource_manager=True)
        self.org = Organization.objects.create(name='ТОО "ALPHA"', tax_id=valid_bin(), short_name="alpha")
        self.t = Tenant.objects.create(organization=self.org, name="proj", title="P", approval="approved", stage="poc")
        self.client.force_login(self.sa)

    def test_edit_form_order_and_stage_alone(self):
        page = self.client.get(reverse("tenant_edit_all", args=[self.t.pk])).content.decode()  # u05: edit is its own page
        order = [page.index(x) for x in (">Organization</legend>", ">Project</legend>", ">Stage, status and review</legend>",
                                          ">People</legend>")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn('name="mv-organization" required', page)  # hidden until "Move to another": not required
        r = self.client.post(reverse("tenant_edit_all", args=[self.t.pk]), {"st-stage": "client", "e-name": "proj", "e-title": "P",
                                                                         "mv-organization": ""})
        self.assertEqual(r.status_code, 302)
        self.t.refresh_from_db()
        self.assertEqual(self.t.stage, "client")

    def test_add_or_invite(self):
        url = reverse("staff_users")
        r = self.client.get(url + "?tab=platform")
        self.assertContains(r, 'data-open="dlg-staff-invite"')
        self.assertContains(r, 'data-open="dlg-person-add"')
        mail.outbox.clear()
        self.client.post(reverse("staff_invite_new"), {"email": "new@corp.kz", "full_name": "New Person", "phone": "",
                                                        "city": "astana", "roles": ["is_support"]})
        u = User.objects.get(email="new@corp.kz")
        self.assertTrue(u.is_support)
        self.assertEqual(mail.outbox[-1].to, ["new@corp.kz"])
        self.client.post(reverse("customer_add"), {"organization": self.org.pk, "full_name": "Asel Nurlanova",
                                                    "email": "asel@alpha.kz", "phone": "+7 701 2345678", "city": "astana"})
        c = User.objects.get(email="asel@alpha.kz")
        self.assertEqual((c.kind, c.organization_id, c.login_enabled), (Kind.CUSTOMER, self.org.pk, False))
        r = self.client.get(url + "?tab=tenant").content.decode()
        self.assertLess(r.index(">Email<"), r.index(">Organization<"))  # email, name, state, organization, projects, roles
        PortalSettings.get()
        PortalSettings.objects.update(users_add=False)  # Settings → Platform: only invited
        r = self.client.get(url + "?tab=tenant")
        self.assertNotContains(r, 'data-open="dlg-customer-add"')
        self.assertContains(r, 'data-open="dlg-customer-invite"')
        self.assertEqual(self.client.post(reverse("person_add"), {"email": "x@corp.kz", "full_name": "X Y", "city": "astana"}).status_code, 403)
        self.client.post(reverse("portal_settings"), {"tab": "platform", "section": "users_add", "users_add": "on"})
        self.assertTrue(PortalSettings.get().users_add)

    def test_copy_icon(self):
        from django.template.loader import render_to_string
        html = render_to_string("ui/copy.html", {"text": "10.0.0.1"})
        self.assertIn("⧉", html)
        self.assertNotIn(">Copy<", html)

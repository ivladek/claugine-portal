"""Release 226 (owner 2026-10-31): updates from GitHub (Settings → Portal → Updates), automatic updates (off by
default; the standby first, the active node only when that worked) and the version under test at
<public URL>/test-new-version/ (accounts/testmount.py)."""
import os
import tempfile
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import autoupdate, ha, testmount
from accounts.tests_ha import FakeNode, NodeFile, otp, staff
from tenants.models import PortalSettings

OLD, NEW = "a" * 40, "b" * 40
GH = {"branch": "main", "installed": OLD, "tags": [{"tag": "v2", "commit": NEW, "date": "2026-10-31"},
                                                    {"tag": "v1", "commit": OLD, "date": "2026-10-30"}],
      "latest": {"tag": "v2", "commit": NEW, "date": "2026-10-31"}}


class AutoUpdate(TestCase):
    def setUp(self):
        staff(is_super_admin=True)
        self.node = NodeFile(SITE="dc1", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="active")
        self.node.__enter__()
        self.addCleanup(self.node.__exit__)

    def run_tick(self, results, gh=GH):
        ops = iter(results)
        with mock.patch("accounts.ha_agent.github_status", return_value=gh), \
                mock.patch("accounts.ha_agent.read_op", return_value={}), \
                mock.patch("accounts.ha_agent.run_op", side_effect=lambda name, **kw: next(ops)) as run:
            out = autoupdate.tick(log=lambda *_a: None, force=True)
        return out, [c.args[0] for c in run.call_args_list]

    def test_off_by_default_and_only_on_the_active_node(self):
        self.assertFalse(PortalSettings.get().auto_update)
        self.assertEqual(self.run_tick([]), ("off", []))
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        self.node.set(ROLE="standby")
        self.assertEqual(self.run_tick([])[1], [])

    def test_the_standby_first_and_the_active_node_only_when_that_worked(self):
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        out, ops = self.run_tick([{"state": "failed", "error": "pip failed"}])
        self.assertEqual(ops, ["update-standby"])  # the active node is not touched
        self.assertIn("standby failed", out)
        self.assertEqual(PortalSettings.get().auto_update_state["failed_tag"], "v2")
        self.assertIn("stopped", mail.outbox[-1].subject)
        out, ops = self.run_tick([])
        self.assertEqual(ops, [])  # v2 is not tried again
        self.assertIn("failed before", out)
        out, ops = self.run_tick([{"state": "done"}, {"state": "done"}],
                                 gh={**GH, "latest": {"tag": "v3", "commit": "c" * 40}})
        self.assertEqual(ops, ["update-standby", "update-active"])
        self.assertEqual(out, "v3: installed on both nodes")
        self.assertEqual(self.run_tick([], gh={**GH, "installed": NEW})[0], "up to date (v2)")

    def test_tasks_running_wait_for_the_next_round(self):
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        from accounts.models import Task
        Task.objects.create(kind="x", title="busy", state="running")
        out, ops = self.run_tick([])
        self.assertEqual((out, ops), ("tasks are running: next round", []))


class AgentUpdates(TestCase):
    def setUp(self):
        from accounts import ha_agent
        self.agent = ha_agent
        state = tempfile.mkdtemp()
        self.patches = [mock.patch.object(ha_agent, "OP_FILE", os.path.join(state, "op.json")),
                        mock.patch.object(ha_agent, "STATE_DIR", state),
                        mock.patch.object(ha_agent, "github_status", return_value=GH),
                        mock.patch.object(ha_agent, "_check_standby", return_value="ok")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_update_active_needs_the_release_on_the_standby_first(self):
        other = FakeNode()
        with NodeFile(SITE="dc1", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="active"), \
                mock.patch.object(self.agent, "peer_request", side_effect=other.request), \
                mock.patch.object(self.agent, "install_ref") as inst, \
                mock.patch.object(self.agent, "_backup_or_fail", return_value="ok"):
            data = self.agent.run_op("update-active", ref="v2")
            self.assertEqual(data["state"], "failed")
            self.assertIn("on the standby first", data["error"])
            inst.assert_not_called()
            data = self.agent.run_op("update-standby", ref="v2")
            self.assertEqual(data["state"], "done", data)
            self.assertIn(("POST", "/install-ref", {"ref": "v2", "migrate": False}), other.calls)
            original = other.request

            def with_commit(*a, **kw):
                r = original(*a, **kw)
                return {**r, "commit": NEW} if a[2] == "/status" else r
            with mock.patch.object(self.agent, "peer_request", side_effect=with_commit):
                data = self.agent.run_op("update-active", ref="v2")
            self.assertEqual(data["state"], "done", data)
            inst.assert_called_once_with("v2", migrate=True)

    def test_op_needs_a_release_tag(self):
        with mock.patch("threading.Thread"):
            self.assertEqual(self.agent.handle("POST", "/op", {"name": "update-standby", "ref": "; rm -rf /"})[0], 400)
            self.assertEqual(self.agent.handle("POST", "/op", {"name": "update-standby", "ref": "v00.06.00"})[0], 202)


class UpdatesTab(TestCase):
    def test_switch_tags_and_the_test_address(self):
        sa = staff(is_super_admin=True)
        self.client.force_login(sa)
        otp(self.client)
        PortalSettings.get() and PortalSettings.objects.update(public_url="https://my.example.com")
        from accounts import appliance
        appliance.forget()
        r = self.client.get(reverse("portal_settings") + "?tab=provider")
        self.assertNotContains(r, 'name="test_url"')  # the Test URL setting is gone
        self.assertNotContains(r, "/test-new-version/")  # u01: the test address is on the Portal tab
        me = {"role": "active", "site": "dc1", "version": "v1", "commit": OLD, "db": {}, "tasks_running": 0, "op": {}}
        other = {"role": "standby", "site": "dc2", "version": "v1", "commit": OLD, "db": {}, "tasks_running": 0, "op": {}}
        with NodeFile(SITE="dc1", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="active"), override_settings(HA_TOKEN="t"), \
                mock.patch("accounts.ha_agent.nodes_status", return_value=[("this node", "10.0.0.25", me), ("other node", "10.0.1.25", other)]), \
                mock.patch("accounts.ha_agent.github_for_page", return_value=GH):
            r = self.client.get(reverse("portal_settings") + "?tab=portal")
            self.assertContains(r, "https://my.example.com/test-new-version/")
            self.assertContains(r, "1. Install on the standby (dc2)")
            self.assertContains(r, '<option value="v2">')
            self.assertNotContains(r, "Install v2 on this node")  # not before the standby runs it
            self.assertNotContains(r, "publish it with a copy of the database")
            other["commit"] = NEW
            r = self.client.get(reverse("portal_settings") + "?tab=portal")
            self.assertContains(r, "2. Install v2 on this node (dc1, active)")
            self.assertContains(r, "2. or: publish it with a copy of the database")
            with mock.patch("accounts.ha_agent.start_op") as start:
                self.client.post(reverse("portal_settings"), {"section": "ha_op", "op": "update-standby", "tab": "portal", "ref": "v2"})
                self.client.post(reverse("portal_settings"), {"section": "ha_op", "op": "update-standby", "tab": "portal", "ref": ""})
            start.assert_called_once_with("update-standby", "sa@corp.kz", ref="v2")
        self.client.post(reverse("portal_settings"), {"section": "auto_update", "tab": "backups", "auto_update": "on"})
        self.assertTrue(PortalSettings.get().auto_update)
        self.client.post(reverse("portal_settings"), {"section": "auto_update", "tab": "backups"})
        self.assertFalse(PortalSettings.get().auto_update)


@override_settings(SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies")
class TestMount(TestCase):
    """The version under test: super admins only; read only on the standby; a release with database changes
    shows a page instead."""

    def setUp(self):
        for p in (mock.patch.object(testmount, "ON", True), mock.patch.object(testmount, "pending_migrations", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        self.node = NodeFile(SITE="dc2", ADDR="10.0.1.25", PEER_ADDR="10.0.0.25", ROLE="standby")
        self.node.__enter__()
        self.addCleanup(self.node.__exit__)

    def test_super_admins_read_only(self):
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("login"), r["Location"])
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)  # not the maintenance page of a standby
        user = staff("rm@corp.kz", is_resource_manager=True)
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse("dashboard")), "Only super admins", status_code=403)
        sa = staff(is_super_admin=True)
        self.client.force_login(sa)
        otp(self.client)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "New version under test")
        self.assertContains(r, "read only")
        r = self.client.post(reverse("portal_settings"), {"section": "auto_update", "auto_update": "on"})
        self.assertContains(r, "read only", status_code=403)
        self.assertFalse(PortalSettings.get().auto_update)
        from accounts.models import AuditLog, audit
        n = AuditLog.objects.count()
        audit(sa, "login", sa)
        self.assertEqual(AuditLog.objects.count(), n)  # nothing written on the standby
        self.node.set(ROLE="new")  # a copy of the database: changes are fine there
        audit(sa, "login", sa)
        self.assertEqual(AuditLog.objects.count(), n + 1)

    def test_a_release_with_database_changes(self):
        with mock.patch.object(testmount, "pending_migrations", return_value=["tenants.0099_x"]):
            r = self.client.get(reverse("login"))
        self.assertContains(r, "Publish the new version", status_code=503)
        self.assertContains(r, "tenants.0099_x", status_code=503)

"""The portal on two sites: node roles, the status page, the role gate, the HA agent's operations (MySQL and the other
node mocked), the Publishing tab."""
import os
import tempfile
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import ha


class NodeFile:
    def __init__(self, **values):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "node.env")
        ha.write_node(values, path=self.path)

    def __enter__(self):
        self.ov = override_settings(NODE_ENV_FILE=self.path)
        self.ov.enable()
        ha.forget()
        return self

    def __exit__(self, *a):
        self.ov.disable()
        ha.forget()

    def set(self, **values):
        ha.write_node(values, path=self.path)
        ha.forget()


def staff(email="sa@corp.kz", **kw):
    from accounts.models import Kind, User
    return User.objects.create_user(email, "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True,
                                    totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True, **kw)


def otp(client):
    s = client.session
    s["otp_verified"] = True
    s.save()


class Roles(TestCase):
    def test_status_page_gate_mail_and_platform_writes(self):
        # no node.env: one active portal, as before
        with override_settings(NODE_ENV_FILE="/nonexistent/node.env"):
            ha.forget()
            self.assertEqual(self.client.get("/active").status_code, 200)
            self.assertEqual(self.client.get("/standby").status_code, 503)
        with NodeFile(SITE="dc3", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="standby") as nf:
            self.assertEqual(self.client.get("/standby").status_code, 200)
            self.assertEqual(self.client.get("/active").status_code, 503)
            r = self.client.get(reverse("login"))
            self.assertContains(r, "under maintenance", status_code=503)  # a standby serves nothing
            nf.set(ROLE="new")
            sa = staff(is_super_admin=True)
            user = staff("rm@corp.kz", is_resource_manager=True)
            self.client.force_login(user)
            r = self.client.get(reverse("dashboard"))
            self.assertContains(r, "Only super admins", status_code=403)  # others are signed out
            self.assertNotIn("_auth_user_id", self.client.session)
            self.client.force_login(sa)
            otp(self.client)
            self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
            # a new version never changes a platform, a router or an iLO, and never mails users
            from tenants.deploy import DeployError, One
            one = One.__new__(One)
            one.key, one.client = "dc1.cloud", mock.Mock()
            with self.assertRaises(DeployError):
                one.call("one.vm.allocate", "x")
            one.client._call.return_value = "<VM/>"
            self.assertEqual(one.call("one.vm.info", 1), "<VM/>")  # reads are fine
            from inventory.sshexec import SshError, run
            with self.assertRaises(SshError):
                run("10.0.0.1", 22, "u", "KEY", "vbash -s")
            from django.core.mail import EmailMessage
            from accounts import appliance
            work = tempfile.mkdtemp()
            with override_settings(EMAIL_FILE_PATH=work), mock.patch("django.core.mail.backends.smtp.EmailBackend.send_messages") as smtp:
                appliance.EmailBackend().send_messages([EmailMessage("s", "b", None, ["x@example.kz"])])
            smtp.assert_not_called()
            self.assertTrue(os.listdir(work))
            # the timers do nothing off the active node
            from io import StringIO
            from django.core.management import call_command
            out = StringIO()
            call_command("collect_platforms", stdout=out)
            self.assertIn("not active", out.getvalue())


class FakeNode:
    """The other node as the agent sees it: its role, version, replication and the calls made to it."""

    def __init__(self, role="standby", version="v1"):
        self.role, self.version, self.calls, self.applied = role, version, [], True

    def request(self, addr, method, path, body=None, raw=False, timeout=60):
        self.calls.append((method, path, body))
        if path == "/status":
            return {"role": self.role, "site": "dc2", "version": self.version,
                    "db": {"replica": {"io": "Yes", "sql": "Yes"} if self.role == "standby" else {}}}
        if path == "/role":
            self.role = body["role"]
            return {"result": f"role {self.role}"}
        if path == "/wait-gtid":
            return {"ok": self.applied}
        return {"result": f"{path} ok"}


class Agent(TestCase):
    def setUp(self):
        from accounts import ha_agent
        self.state = tempfile.mkdtemp()
        self.patches = [mock.patch.object(ha_agent, "OP_FILE", os.path.join(self.state, "op.json")),
                        mock.patch.object(ha_agent, "STATE_DIR", self.state),
                        mock.patch.object(ha_agent, "version", return_value="v1"),
                        mock.patch.object(ha_agent, "sql1", return_value="uuid:1-100,\\nuuid2:1-3"),
                        mock.patch.object(ha_agent, "sql", return_value=[]),
                        mock.patch.object(ha_agent, "replica_status", return_value={})]
        for p in self.patches:
            p.start()
        self.agent = ha_agent

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_switchover_and_its_refusals(self):
        other = FakeNode()
        with NodeFile(SITE="dc1", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="active"), \
                mock.patch.object(self.agent, "peer_request", side_effect=other.request), \
                mock.patch.object(self.agent, "set_read_only") as ro, \
                mock.patch.object(self.agent, "start_replica") as rep, mock.patch("time.sleep"):
            data = self.agent.run_op("switchover", by="sa@corp.kz")
            self.assertEqual(data["state"], "done", data)
            self.assertEqual(ha.read_node()["ROLE"], "standby")  # this node now follows the other one
            self.assertEqual(other.role, "active")
            ro.assert_called_with(True)
            rep.assert_called_once_with("10.0.1.25")
            waited = [b for _m, p, b in other.calls if p == "/wait-gtid"][0]
            self.assertEqual(waited["gtid"], "uuid:1-100,uuid2:1-3")  # one line, as WAIT_FOR_EXECUTED_GTID_SET takes it
            paths = [p for _m, p, _b in other.calls]
            self.assertLess(paths.index("/wait-gtid"), paths.index("/promote"))  # caught up before it takes over
            self.assertIn("/note", paths)  # the log entry is written on the new active node
        # refused: another version on the other node, or a running task
        other = FakeNode(version="v2")
        with NodeFile(SITE="dc1", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="active"), \
                mock.patch.object(self.agent, "peer_request", side_effect=other.request):
            data = self.agent.run_op("switchover")
            self.assertEqual(data["state"], "failed")
            self.assertIn("another version", data["error"])
            self.assertEqual(ha.read_node()["ROLE"], "active")
        other = FakeNode()
        other.applied = False  # the standby does not catch up: this node stays active
        with NodeFile(SITE="dc1", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="active"), \
                mock.patch.object(self.agent, "peer_request", side_effect=other.request), \
                mock.patch.object(self.agent, "set_read_only") as ro, mock.patch("time.sleep"):
            data = self.agent.run_op("switchover")
            self.assertEqual(data["state"], "failed")
            self.assertEqual(ha.read_node()["ROLE"], "active")
            ro.assert_called_with(False)
            self.assertEqual(other.role, "standby")

    def test_new_version_published_and_made_active(self):
        other = FakeNode()
        with NodeFile(SITE="dc1", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="active"), \
                mock.patch.object(self.agent, "peer_request", side_effect=other.request):
            self.assertEqual(self.agent.run_op("to-new")["state"], "done")
            self.assertEqual(other.role, "new")
            self.assertIn(("POST", "/copy", {"source": "10.0.0.25"}), other.calls)
            with mock.patch.object(self.agent, "set_read_only"), mock.patch.object(self.agent, "install_from") as inst, \
                    mock.patch.object(self.agent, "load_from") as load:
                data = self.agent.run_op("new-to-active")
            self.assertEqual(data["state"], "done", data)
            self.assertEqual((other.role, ha.read_node()["ROLE"]), ("active", "standby"))
            inst.assert_called_once_with("10.0.1.25")  # this node takes the new version
            load.assert_called_once_with("10.0.1.25", replicate=True)

    def test_agent_needs_the_token(self):
        with override_settings(HA_TOKEN="s3cret-token"):
            self.assertTrue(self.agent.check_token("s3cret-token"))
            self.assertFalse(self.agent.check_token("wrong"))
            self.assertFalse(self.agent.check_token(""))
        with override_settings(HA_TOKEN=""):
            self.assertFalse(self.agent.check_token(""))


class Publishing(TestCase):
    def test_tab_lists_nodes_and_offers_the_switchover(self):
        sa = staff(is_super_admin=True)
        self.client.force_login(sa)
        otp(self.client)
        me = {"role": "active", "site": "dc1", "version": "v1", "db": {"read_only": False, "replica": {}}, "tasks_running": 0, "op": {}}
        other = {"role": "standby", "site": "dc2", "version": "v1", "tasks_running": 0, "op": {},
                 "db": {"read_only": True, "replica": {"source": "10.0.0.25", "io": "Yes", "sql": "Yes", "behind": "0", "error": ""}}}
        with NodeFile(SITE="dc1", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="active", LB_ADDRS="10.0.0.24,10.0.1.24"), \
                override_settings(HA_TOKEN="t"), \
                mock.patch("accounts.ha_agent.nodes_status", return_value=[("this node", "10.0.0.25", me), ("other node", "10.0.1.25", other)]), \
                mock.patch("accounts.ha_agent.github_for_page", return_value={"error": "no GitHub in tests"}):
            r = self.client.get(reverse("portal_settings") + "?tab=publishing")
            self.assertContains(r, "Switch over to dc2")
            self.assertNotContains(r, "publish it with a copy of the database")  # u01: only when the standby runs another version
            self.assertContains(r, "replicates from")
            with mock.patch("accounts.ha_agent.start_op") as start:
                self.client.post(reverse("portal_settings"), {"section": "ha_op", "op": "switchover", "tab": "publishing"})
            start.assert_called_once_with("switchover", "sa@corp.kz", ref="")
            from accounts.models import Task
            Task.objects.create(kind="x", title="busy", state="running")
            with mock.patch("accounts.ha_agent.start_op") as start:
                r = self.client.post(reverse("portal_settings"), {"section": "ha_op", "op": "switchover", "tab": "publishing"}, follow=True)
            start.assert_not_called()
            self.assertContains(r, "tasks are running")


class DirectMail(TestCase):
    def test_postfix_delivers_itself_under_the_node_mail_name(self):
        from accounts import appliance
        from tenants.models import PortalSettings
        s = PortalSettings.objects.first() or PortalSettings.objects.create()
        s.mail_mode, s.mail_from, s.mail_host = "direct", "Example Cloud <portal@my.claugine.com>", ""
        conf = {"relayhost": "[smtp-relay.brevo.com]:587", "myhostname": "app.claugine.com", "inet_interfaces": "loopback-only"}
        runs = []

        def run(cmd, **kw):
            runs.append(cmd)
            if cmd[:2] == ["postconf", "-e"]:
                conf.update(x.split("=", 1) for x in cmd[2:])
            return mock.Mock(returncode=0, stdout="")
        with NodeFile(SITE="dc1", ADDR="10.0.0.25", ROLE="active", MAIL_NAME="service2-dc1.provider.example"), \
                mock.patch("shutil.which", return_value="/usr/sbin/postconf"), \
                mock.patch.object(appliance, "_postconf", side_effect=lambda k: conf.get(k, "")), \
                mock.patch("subprocess.run", side_effect=run):
            log = []
            appliance.apply_mail(s, log.append)
            self.assertEqual((conf["relayhost"], conf["myhostname"], conf["myorigin"], conf["mydestination"]),
                             ("", "service2-dc1.provider.example", "$myhostname", "$myhostname, localhost"))
            self.assertIn(["systemctl", "restart", "postfix"], runs)
            runs.clear()
            appliance.apply_mail(s, log.append)  # nothing differs: nothing changes
            self.assertEqual(runs, [])


class PlatformPublicEndpoints(TestCase):
    def test_one_url_with_ports_and_one_for_vms(self):
        from inventory.models import Platform
        p = Platform(key="dc1.cloud", name="cloud", kind="shared", public_url="https://one-dc1.claugine.com/",
                     fireedge_port=55516, grpc_port=55534, xmlrpc_port=55533,
                     vm_url="https://one-dc1.claugine.com", onegate_port=55530, oneflow_port=55574)
        self.assertEqual(p.endpoints, [("FireEdge", "https://one-dc1.claugine.com:55516"),
                                       ("gRPC API", "https://one-dc1.claugine.com:55534"),
                                       ("XML-RPC API", "https://one-dc1.claugine.com:55533/RPC2"),
                                       ("OneGate", "https://one-dc1.claugine.com:55530"),
                                       ("OneFlow", "https://one-dc1.claugine.com:55574")])
        self.assertEqual(p.fireedge_link, "https://one-dc1.claugine.com:55516")
        p.fireedge_port = None  # no port: the URL as it is
        p.public_url = "https://dc2-one.provider.example"
        self.assertEqual(p.fireedge_link, "https://dc2-one.provider.example")
        self.assertEqual(Platform(key="x.y", name="y", kind="shared").endpoints, [])


class ExtManagedKind(TestCase):
    def test_old_term_portal_still_loads_as_ext_managed(self):
        from inventory import tree
        self.assertEqual(tree.kind_term("portal"), "ext-managed")
        self.assertEqual(tree.kind_term("shared"), "shared")
        self.assertIn("ext-managed", tree.PLATFORM_KINDS)
        self.assertNotIn("portal", tree.PLATFORM_KINDS)
        self.assertEqual(tree.CLUSTER_KINDS_FOR["ext-managed"], {"ext-managed"})

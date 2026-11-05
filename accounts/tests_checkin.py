"""owner 2026-11-04 (migration to licensed updates): the portal's pull check-in client and the tick() that drives the
HA update from the control plane's answer (with the GitHub fallback and the no-downgrade guard)."""
import contextlib
import urllib.error
from unittest import mock

from django.test import TestCase, override_settings

from accounts import autoupdate, checkin
from tenants.models import PortalSettings


class Configured(TestCase):
    def test_configured_needs_all_three(self):
        with self.settings(CHECKIN_URL="https://cp.example", LICENCE_KEY="k", INSTALL_ID="i"):
            self.assertTrue(checkin.configured())
        with self.settings(CHECKIN_URL="", LICENCE_KEY="k", INSTALL_ID="i"):
            self.assertFalse(checkin.configured())
        with self.settings(CHECKIN_URL="https://cp.example", LICENCE_KEY="", INSTALL_ID="i"):
            self.assertFalse(checkin.configured())


@override_settings(CHECKIN_URL="https://cp.example", LICENCE_KEY="k", INSTALL_ID="i")
class CheckInClient(TestCase):
    def test_200_is_ok_with_data(self):
        payload = b'{"state":"active","target_version":"v00.11.17","notices":[]}'
        resp = mock.MagicMock()
        resp.read.return_value = payload
        resp.__enter__.return_value = resp
        with mock.patch("urllib.request.urlopen", return_value=resp):
            ok, data = checkin.check_in("v00.11.16", "stable", {})
        self.assertTrue(ok)
        self.assertEqual(data["target_version"], "v00.11.17")

    def test_401_unknown_licence_is_outage_not_state(self):
        err = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            ok, data = checkin.check_in("v00.11.16", "stable", {})
        self.assertFalse(ok)
        self.assertEqual(data["http"], 401)

    def test_network_error_is_outage(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")):
            ok, data = checkin.check_in("v00.11.16", "stable", {})
        self.assertFalse(ok)
        self.assertIn("error", data)


class Tick(TestCase):
    def setUp(self):
        s = PortalSettings.get()
        s.auto_update, s.update_stream, s.auto_update_state = True, "stable", {}
        s.save()

    @contextlib.contextmanager
    def _env(self, checkin_result):
        gh = {"installed": "c1", "installed_tag": "v00.11.16", "latest": {"tag": "v00.11.99", "commit": "c9"}}
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch("accounts.ha.role", return_value="active"))
            st.enter_context(mock.patch("accounts.ha_agent.github_status", return_value=gh))
            st.enter_context(mock.patch("accounts.ha_agent.read_op", return_value={}))
            self.run_op = st.enter_context(mock.patch("accounts.ha_agent.run_op", return_value={"state": "done"}))
            st.enter_context(mock.patch("accounts.tasks.running_count", return_value=0))
            st.enter_context(mock.patch("accounts.checkin.configured", return_value=True))
            st.enter_context(mock.patch("accounts.checkin.usage", return_value={}))
            st.enter_context(mock.patch("accounts.checkin.check_in", return_value=checkin_result))
            yield

    def _state(self):
        return PortalSettings.get().auto_update_state or {}

    def test_newer_target_runs_ha_update(self):
        with self._env((True, {"state": "active", "target_version": "v00.11.17", "notices": []})):
            out = autoupdate.tick(force=True)
        self.assertIn("installed on both nodes", out)
        self.assertEqual([c.kwargs["ref"] for c in self.run_op.call_args_list], ["v00.11.17", "v00.11.17"])

    def test_no_downgrade_guard(self):
        with self._env((True, {"state": "active", "target_version": "v00.11.15"})):
            out = autoupdate.tick(force=True)
        self.assertIn("no downgrade", out)
        self.run_op.assert_not_called()

    def test_frozen_freezes_updates_and_stores_notice(self):
        with self._env((True, {"state": "frozen", "target_version": None, "notices": ["Licence X ended."]})):
            out = autoupdate.tick(force=True)
        self.assertIn("frozen", out)
        self.run_op.assert_not_called()
        st = self._state()
        self.assertEqual(st["lic_state"], "frozen")
        self.assertEqual(st["notices"], ["Licence X ended."])

    def test_conflict_freezes_updates(self):
        with self._env((True, {"state": "conflict", "target_version": None, "notices": ["bound elsewhere"]})):
            out = autoupdate.tick(force=True)
        self.assertIn("conflict", out)
        self.run_op.assert_not_called()

    def test_outage_keeps_state_no_update(self):
        with self._env((False, {"http": 401})):
            out = autoupdate.tick(force=True)
        self.assertIn("unreachable", out)
        self.run_op.assert_not_called()
        self.assertNotIn("lic_state", self._state())  # state untouched on an outage

    def test_grace_still_updates(self):
        with self._env((True, {"state": "grace", "target_version": "v00.11.17", "grace_until": "2026-12-31",
                               "notices": ["in grace"]})):
            out = autoupdate.tick(force=True)
        self.assertIn("installed on both nodes", out)
        self.assertEqual(self._state()["lic_state"], "grace")

    def test_github_fallback_when_not_configured(self):
        """No check-in configured → the original newest-tag behaviour."""
        gh = {"installed": "c1", "installed_tag": "v00.11.16", "latest": {"tag": "v00.11.99", "commit": "c9"}}
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch("accounts.ha.role", return_value="active"))
            st.enter_context(mock.patch("accounts.ha_agent.github_status", return_value=gh))
            st.enter_context(mock.patch("accounts.ha_agent.read_op", return_value={}))
            run_op = st.enter_context(mock.patch("accounts.ha_agent.run_op", return_value={"state": "done"}))
            st.enter_context(mock.patch("accounts.tasks.running_count", return_value=0))
            st.enter_context(mock.patch("accounts.checkin.configured", return_value=False))
            out = autoupdate.tick(force=True)
        self.assertIn("installed on both nodes", out)
        self.assertEqual(run_op.call_args_list[0].kwargs["ref"], "v00.11.99")
        self.assertEqual(self._state().get("source"), "github")

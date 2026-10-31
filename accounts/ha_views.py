"""Settings → Publishing: the portal's public names and its nodes on two sites (accounts/ha.py, accounts/ha_agent.py)."""
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from .models import audit
from . import ha

OP_LABELS = {
    "switchover": _l("Switch over"),
    "to-new": _l("Publish the new version"),
    "new-to-active": _l("Make the new version active"),
    "new-to-standby": _l("Back to standby"),
    "maintenance-on": _l("Maintenance on"),
    "maintenance-off": _l("Maintenance off"),
    "update-standby": _l("Install a release on the standby"),
    "update-active": _l("Install the release on the active node"),
    "standby-back": _l("Standby back to the active node's version"),
}
TEST_PATH = "/test-new-version/"  # owner 2026-10-31: the standby's version under the public URL (HAProxy, Apache)


def test_url():
    from . import appliance
    return appliance.public_url().rstrip("/") + TEST_PATH


def _back(tab="portal"):  # corrections-18: public names under Provider, the nodes under Backups
    return redirect(reverse("portal_settings") + f"?tab={tab}")


def post(request, obj):
    if not request.user.is_super_admin:
        raise PermissionDenied
    if request.POST.get("section") == "auto_update":  # owner 2026-10-31: off by default
        on = request.POST.get("auto_update") == "on"
        if on != obj.auto_update:
            obj.auto_update, obj.updated_by = on, request.user
            if on:  # switched on again: a release that failed before is tried again
                obj.auto_update_state = {**(obj.auto_update_state or {}), "failed_tag": "", "checked_at": ""}
            obj.save()
            audit(request.user, "setting_changed", obj, auto_update=on)
        messages.success(request, _("Automatic updates on.") if on else _("Automatic updates off."))
        return _back()
    if request.POST.get("section") == "publishing":
        changed = {}
        for f in ("public_url",):  # the test address is the public URL + /test-new-version/ (release 226)
            v = (request.POST.get(f) or "").strip().rstrip("/")
            if v:
                try:
                    URLValidator(schemes=["https", "http"])(v)
                except ValidationError:
                    messages.error(request, _("Not a URL: %(u)s") % {"u": v})
                    return _back("provider")
            if v != getattr(obj, f):
                setattr(obj, f, v)
                changed[f] = v
        if changed:
            obj.updated_by = request.user
            obj.save()
            from . import appliance
            appliance.forget()
            audit(request.user, "setting_changed", obj, **changed)
        messages.success(request, _("Settings saved."))
        return _back("provider")
    name = request.POST.get("op", "")
    if name not in OP_LABELS:
        raise PermissionDenied
    from . import ha_agent, tasks
    if name in ("switchover", "new-to-active") and tasks.running_count():
        messages.error(request, _("Not now: tasks are running (see Tasks). Wait until they finish."))
        return _back()
    ref = (request.POST.get("ref") or "").strip()
    if name in ha_agent.REF_OPS and not ha_agent.REF_RE.match(ref):
        messages.error(request, _("Choose a release."))
        return _back()
    if name == "update-active" and tasks.running_count():
        messages.error(request, _("Not now: tasks are running (see Tasks). Wait until they finish."))
        return _back()
    try:
        ha_agent.start_op(name, request.user.email, ref=ref if name in ha_agent.REF_OPS else "")
    except ha_agent.AgentError as exc:
        messages.error(request, _("The operation did not start: %(e)s") % {"e": exc})
        return _back()
    audit(request.user, f"ha_{name.replace('-', '_')}_requested", obj, **({"ref": ref} if ref else {}))
    messages.success(request, _("Started: %(o)s. The steps are shown below; the portal may be unavailable for a "
                                "minute while it switches.") % {"o": OP_LABELS[name]})
    return _back()


def context(request, obj):
    from . import ha_agent, tasks
    n = ha.node()
    nodes = ha_agent.nodes_status() if (n.get("ADDR") and settings.HA_TOKEN) else []
    me = next((s for label, _a, s in nodes if label == "this node" and "error" not in s), None)
    other = next((s for label, _a, s in nodes if label == "other node" and "error" not in s), None)
    ops = []
    if me and other and request.user.is_super_admin:
        mine, theirs = me.get("role"), other.get("role")
        same = me.get("version") == other.get("version")
        if mine == "active" and theirs == "standby":
            ops.append(("switchover", _("Switch over to %(s)s") % {"s": other.get("site")}, same,
                        "" if same else _("the other node runs another version")))
            ops.append(("maintenance-on", _("Maintenance on %(s)s") % {"s": other.get("site")}, True, ""))
        if mine == "active" and theirs == "maintenance":
            ops.append(("maintenance-off", _("Maintenance off on %(s)s") % {"s": other.get("site")}, True, ""))
    updates = _updates(request, me, other)
    busy = tasks.running_count()
    return {**updates, "test_url": test_url(), "auto_update": obj.auto_update, "auto_update_state": obj.auto_update_state or {},
            "ha_node": n, "ha_role": ha.role(), "ha_nodes": nodes, "ha_ops": ops, "ha_busy": busy,
            "ha_lbs": [x for x in (n.get("LB_ADDRS") or "").replace(",", " ").split() if x],
            "ha_running": any((s.get("op") or {}).get("state") == "running" for _l, _a, s in nodes if "error" not in s)}


def _updates(request, me, other):
    """Settings → Portal → Updates: the releases on GitHub, what each node runs, what can be installed now."""
    from . import ha_agent
    if not (me and request.user.is_super_admin):
        return {"gh": {}, "gh_ops": []}
    gh = ha_agent.github_for_page()
    tags = [] if gh.get("error") else (gh.get("tags") or [])
    by_commit = {t["commit"]: t["tag"] for t in tags}
    me["tag"] = by_commit.get(me.get("commit", ""), "")
    if other:
        other["tag"] = by_commit.get(other.get("commit", ""), "")
    # u01: the steps of a new version, in order: (operation, label, releases, what it does)
    ops = []
    if me.get("role") == "active" and other and other.get("role") == "standby":
        newer = [t for t in tags if t["commit"] != me.get("commit")]
        if newer:
            ops.append(("update-standby", _("1. Install on the standby (%(s)s)") % {"s": other.get("site")}, newer,
                        _("From GitHub, without database changes; then try it at the test address.")))
        if other.get("commit") and other.get("commit") != me.get("commit"):
            if other.get("tag"):
                ops.append(("update-active", _("2. Install %(t)s on this node (%(s)s, active)") % {"t": other["tag"], "s": me.get("site")},
                            [{"tag": other["tag"]}], _("A backup, then the release with its database changes. No tasks may run.")))
            ops.append(("to-new", _("2. or: publish it with a copy of the database"), [],
                        _("For a release with database changes: the standby takes a copy of the database with the changes "
                          "(no replication) and becomes the new version under test; then make it active or bring it back.")))
            ops.append(("standby-back", _("Back: the standby to this node's version"), [], _("From GitHub, the commit this node runs.")))
    if me.get("role") == "active" and other and other.get("role") == "new":
        ops.append(("new-to-active", _("3. Make the new version on %(s)s active") % {"s": other.get("site")}, [],
                    _("This node stops (maintenance), the new one takes a final copy and becomes active; this node installs that "
                      "version and becomes its standby.")))
        ops.append(("new-to-standby", _("Back: the new version back to standby"), [],
                    _("It installs this node's version again and replicates from it.")))
    return {"gh": gh, "gh_ops": ops, "gh_latest": (gh.get("latest") or {}).get("tag", ""),
            "gh_installed_tag": me.get("tag", "")}

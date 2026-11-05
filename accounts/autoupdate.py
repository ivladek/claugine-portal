"""Automatic updates of the portal (owner 2026-10-31): Settings → Portal → Updates.

Off by default. When on, the active node checks GitHub every 10 minutes (from the appliance timer, as root) for a newer
release (a tag v… on the branch of dist.env) and installs it on both nodes, the standby first:

    1. update-standby   the standby installs the release from GitHub (no database changes there)
    2. update-active    only when 1. succeeded: no tasks running (else it waits for the next round), a backup, then
                        this node installs the release from GitHub with the database changes

When a step fails, nothing more is done for that release (the active node is not touched when the standby failed), the
super admins get an e-mail, and auto-update waits for a newer release. The state (last check, release, result) is kept
in PortalSettings.auto_update_state and shown in Settings.
"""
from datetime import datetime, timedelta

from django.utils import timezone

EVERY = timedelta(minutes=10)


def _settings():
    from tenants.models import PortalSettings
    return PortalSettings.get()


def _save(**state):
    from tenants.models import PortalSettings
    s = _settings()
    new = {**(s.auto_update_state or {}), **state}
    PortalSettings.objects.filter(pk=s.pk).update(auto_update_state=new)
    return new


def _notify(subject, text):
    from accounts.models import Kind, User
    from accounts.utils import notify
    admins = list(User.objects.filter(kind=Kind.STAFF, is_super_admin=True, is_active=True))
    if admins:
        notify(admins, "auto_update_failed", {"text": text}, subject)


def _vkey(tag):
    """A comparable tuple from a vMM.mm.pp tag, for the no-downgrade guard."""
    import re
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(x) for x in nums[:4]) or (0,)


def tick(log=print, force=False):
    """One round (the appliance timer, as root). Returns what it did, for the log.

    The target release comes from the licence control plane (cp.example.com) when the installer configured a check-in
    (CHECKIN_URL/LICENCE_KEY/INSTALL_ID), otherwise from the newest GitHub release tag (the original behaviour). The HA
    install — standby first, then the active node — is the same either way."""
    from accounts import ha, ha_agent
    s = _settings()
    if not s.auto_update or ha.role() != "active":
        return "off" if not s.auto_update else f"not the active node ({ha.role()})"
    state = s.auto_update_state or {}
    last = state.get("checked_at")
    now = timezone.now()
    if not force and last and now - datetime.fromisoformat(last) < EVERY:
        return "checked recently"
    gh = ha_agent.github_status()
    installed = gh.get("installed")
    current_tag = gh.get("installed_tag") or ""

    from accounts import checkin
    if checkin.configured():
        ok, data = checkin.check_in(current_tag, s.update_stream, checkin.usage())
        if not ok:  # outage / 401 unknown_licence / 400 bad_request → keep the current state, do not update
            _save(checked_at=now.isoformat())
            return f"check-in unreachable ({data.get('http') or data.get('error') or '?'})"
        lic_state = data.get("state") or "active"
        _save(checked_at=now.isoformat(), source="control-plane", lic_state=lic_state,
              grace_until=data.get("grace_until") or "", notices=list(data.get("notices") or []),
              usage_flags=list(data.get("usage_flags") or []), latest=data.get("target_version") or "")
        if lic_state in ("frozen", "conflict"):
            return f"licence {lic_state}: updates frozen"
        tag = data.get("target_version") or ""
        if not tag or tag == current_tag:
            return f"up to date ({current_tag or 'unknown'})"
        if _vkey(tag) <= _vkey(current_tag):  # never step back: HA migrations are forward-only
            return f"target {tag} not newer than {current_tag}: skipped (no downgrade)"
        if tag == state.get("failed_tag"):
            return f"{tag} failed before: waiting for a newer release"
    else:
        latest = gh.get("latest") or {}
        _save(checked_at=now.isoformat(), source="github", latest=latest.get("tag", ""))
        if not latest:
            return "no release on GitHub"
        if latest["commit"] == installed:
            return f"up to date ({latest['tag']})"
        if latest["tag"] == state.get("failed_tag"):
            return f"{latest['tag']} failed before: waiting for a newer release"
        tag = latest["tag"]

    if ha_agent.read_op().get("state") == "running":
        return "another operation is running"
    from accounts.tasks import running_count
    if running_count():
        return "tasks are running: next round"
    log(f"auto-update: {tag} onto the standby")
    op = ha_agent.run_op("update-standby", by="auto-update", ref=tag)
    if op.get("state") != "done":
        _save(failed_tag=tag, result=f"{tag}: the standby failed: {op.get('error', '')}"[:500], at=now.isoformat())
        _notify(f"Auto-update to {tag} stopped: the standby failed",
                f"The standby could not install {tag}: {op.get('error', '')}\nThe active node was not touched.")
        return f"{tag}: the standby failed"
    log(f"auto-update: {tag} onto the active node")
    op = ha_agent.run_op("update-active", by="auto-update", ref=tag)
    if op.get("state") != "done":
        _save(failed_tag=tag, result=f"{tag}: the active node failed: {op.get('error', '')}"[:500], at=now.isoformat())
        _notify(f"Auto-update to {tag} stopped: the active node failed",
                f"The standby runs {tag}; the active node could not install it: {op.get('error', '')}")
        return f"{tag}: the active node failed"
    _save(result=f"{tag}: installed on both nodes", at=now.isoformat(), failed_tag="")
    return f"{tag}: installed on both nodes"

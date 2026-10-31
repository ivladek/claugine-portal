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


def tick(log=print, force=False):
    """One round (the appliance timer, as root, every 5 minutes). Returns what it did, for the log."""
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
    latest = gh.get("latest") or {}
    _save(checked_at=now.isoformat(), latest=latest.get("tag", ""))
    if not latest:
        return "no release on GitHub"
    if latest["commit"] == gh.get("installed"):
        return f"up to date ({latest['tag']})"
    if latest["tag"] == state.get("failed_tag"):
        return f"{latest['tag']} failed before: waiting for a newer release"
    if ha_agent.read_op().get("state") == "running":
        return "another operation is running"
    from accounts.tasks import running_count
    if running_count():
        return "tasks are running: next round"
    tag = latest["tag"]
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

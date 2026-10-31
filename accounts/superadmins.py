"""Correction 16: super admins.

A super admin has every right whatever roles are ticked; the roles ticked decide which approvals and notices come
to them (a super admin without the resource manager role is not asked to approve as one). Nobody is bound to being
the first user: every super admin's roles can be edited, also their own, as long as every role keeps an active user.

The super admin role itself:
    - nobody takes it from themselves;
    - another super admin asks to take it away; every super admin is told;
    - it is taken away only when every other super admin (but the one asking and the one concerned) confirms within
      48 hours; one refusal ends the request, and so does the deadline.
"""
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import Kind, SuperAdminRemoval, User, audit


def active_super_admins():
    from .roles import activated
    return [u for u in User.objects.filter(kind=Kind.STAFF, is_super_admin=True) if activated(u)]


def needed(req):
    """The super admins whose confirmation is still needed (now)."""
    return [u for u in active_super_admins() if u.pk not in (req.target_id, req.requested_by_id)
            and u.pk not in (req.confirmed or [])]


def deciders(req):
    """Every super admin who decides (confirmed or not)."""
    return [u for u in active_super_admins() if u.pk not in (req.target_id, req.requested_by_id)]


def pending():
    expire_due()
    return list(SuperAdminRemoval.objects.filter(state=SuperAdminRemoval.State.PENDING).select_related("target", "requested_by"))


def pending_for(user):
    return next((r for r in pending() if r.target_id == user.pk), None)


def _tell(req, template, subject, **extra):
    from .utils import notify, site_link
    people = {u.pk: u for u in active_super_admins()}
    people.setdefault(req.target_id, req.target)
    notify(list(people.values()), template, {"req": req, "link": site_link(reverse("staff_users") + "?tab=platform"),
                                             "hours": SuperAdminRemoval.HOURS, **extra}, subject)


def request(actor, target):
    """Ask to take the super admin role from `target`. Returns (request or None, error)."""
    if not actor.is_super_admin:
        return None, _("Only a super admin can do this.")
    if target.pk == actor.pk:
        return None, _("You cannot take the super admin role from yourself.")
    if not target.is_super_admin:
        return None, _("%(e)s is not a super admin.") % {"e": target.email}
    old = pending_for(target)
    if old is not None:
        return old, ""
    req = SuperAdminRemoval.objects.create(target=target, requested_by=actor, confirmed=[])
    audit(actor, "super_admin_removal_requested", target, request=req.pk, deciders=", ".join(u.email for u in deciders(req)))
    _tell(req, "super_admin_removal_requested",
          _("Super admin role of %(n)s: removal requested") % {"n": target.full_name or target.email},
          deciders=deciders(req))
    complete_if_ready(req, actor)
    return req, ""


def confirm(actor, req):
    if req.state != SuperAdminRemoval.State.PENDING or not actor.is_super_admin:
        return _("This request is closed.")
    expire_due()
    req.refresh_from_db()
    if req.state != SuperAdminRemoval.State.PENDING:
        return _("This request is closed.")
    if actor.pk not in [u.pk for u in deciders(req)]:
        return _("Your confirmation is not needed.")
    if actor.pk not in (req.confirmed or []):
        req.confirmed = list(req.confirmed or []) + [actor.pk]
        req.save(update_fields=["confirmed"])
        audit(actor, "super_admin_removal_confirmed", req.target, request=req.pk)
    complete_if_ready(req, actor)
    return ""


def decline(actor, req):
    if req.state != SuperAdminRemoval.State.PENDING or not actor.is_super_admin:
        return _("This request is closed.")
    if actor.pk not in [u.pk for u in deciders(req)] and actor.pk != req.requested_by_id:
        return _("Your confirmation is not needed.")
    _finish(req, SuperAdminRemoval.State.DECLINED, actor)
    audit(actor, "super_admin_removal_declined", req.target, request=req.pk)
    _tell(req, "super_admin_removal_closed",
          _("Super admin role of %(n)s: removal declined") % {"n": req.target.full_name or req.target.email}, by=actor)
    return ""


def _finish(req, state, by=None):
    req.state, req.finished_at, req.finished_by = state, timezone.now(), by
    req.save(update_fields=["state", "finished_at", "finished_by"])


def complete_if_ready(req, actor=None):
    """Take the role away when nobody's confirmation is missing (and a super admin stays)."""
    from .roles import missing
    if req.state != SuperAdminRemoval.State.PENDING or needed(req):
        return False
    target = User.objects.get(pk=req.target_id)
    with transaction.atomic():
        target.is_super_admin = False
        others = [target if u.pk == target.pk else u for u in User.objects.filter(kind=Kind.STAFF)]
        if "is_super_admin" in missing(others):
            return False
        # the roles the super admin held only through the flag stay as they are ticked
        target.is_staff = target.is_superuser = False
        target.save(update_fields=["is_super_admin", "is_staff", "is_superuser"])
        _finish(req, SuperAdminRemoval.State.DONE, actor)
    audit(actor, "super_admin_removed", target, request=req.pk,
          confirmed=", ".join(u.email for u in User.objects.filter(pk__in=req.confirmed or [])))
    _tell(req, "super_admin_removal_closed",
          _("Super admin role of %(n)s: removed") % {"n": target.full_name or target.email}, by=actor, done=True)
    return True


def expire_due():
    """Requests past their 48 hours lapse (the role stays)."""
    now = timezone.now()
    for req in SuperAdminRemoval.objects.filter(state=SuperAdminRemoval.State.PENDING).select_related("target"):
        if now >= req.deadline:
            _finish(req, SuperAdminRemoval.State.EXPIRED)
            audit(None, "super_admin_removal_expired", req.target, request=req.pk)
            _tell(req, "super_admin_removal_closed",
                  _("Super admin role of %(n)s: removal lapsed") % {"n": req.target.full_name or req.target.email},
                  expired=True)

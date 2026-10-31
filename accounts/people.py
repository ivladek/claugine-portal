"""People of the portal: platform users (staff) and tenant users (customers), their state and roles.

State
    active    the invitation was accepted: they can sign in
    blocked   the invitation was accepted, sign-in is locked
    invited   an invitation was sent and is not accepted yet
    inactive  not invited yet (a directory entry)
    archived  had an account; removed from the lists, kept for the record without roles; can be invited back

Someone who never accepted an invitation (invited, inactive) is deleted, not archived. Platform users get their
roles with the invitation (no role before it, none after an invitation is revoked). Tenant users are organization
admins (everything of the organization), tenant admins (only the tenants given) or read only (see every tenant of
the organization, change nothing). Platform users must use two-factor authentication; tenant users choose.
"""
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from .models import Invitation, Kind, User, audit

STAFF_ROLES = [
    ("is_super_admin", _l("Super admin")),
    ("is_resource_manager", _l("Resource manager")),
    ("is_support", _l("Support engineer")),
    ("is_sales", _l("Sales manager")),
    ("is_kam", _l("Key account manager")),
]
CUSTOMER_ROLES = [("org_admin", _l("Org admin")), ("tenant_admin", _l("Project admin")), ("read_only", _l("Read only"))]
STATE_LABELS = {"active": _l("active"), "blocked": _l("blocked"), "invited": _l("invited"), "inactive": _l("inactive"),
                "archived": _l("archived")}


def state(u, invited_ids=frozenset(), invited_emails=frozenset()):
    if u.archived_at:
        return "archived"
    accepted = u.login_enabled and u.has_usable_password()
    if accepted:
        return "active" if u.is_active else "blocked"
    if u.pk in invited_ids or u.email.lower() in invited_emails:
        return "invited"
    return "inactive"


def never_accepted(u):
    """Never got in: no accepted invitation (no usable password with sign-in on) and never signed in — or a tenant
    user who registered but was never approved (no organization: the join request was never accepted)."""
    if u.kind == Kind.CUSTOMER and not u.organization_id and not u.memberships.exists():
        return True
    return u.last_login is None and not (u.login_enabled and u.has_usable_password())


def _open_invites(users):
    open_ = Invitation.objects.open().filter(accepted_at__isnull=True)
    ids = set(open_.filter(user__in=users).values_list("user_id", flat=True))
    emails = {e.lower() for e in open_.filter(email__in=[u.email for u in users]).values_list("email", flat=True)}
    return ids, emails


def staff_rows(show_archived=False):
    users = list(User.objects.filter(kind=Kind.STAFF).order_by("-is_super_admin", "full_name", "email"))
    ids, emails = _open_invites(users)
    rows = []
    for u in users:
        st = state(u, ids, emails)
        if st == "archived" and not show_archived:
            continue
        rows.append({"u": u, "state": st, "label": STATE_LABELS[st], "deletable": never_accepted(u),
                     "roles": [(f, label, getattr(u, f) and st not in ("inactive", "archived")) for f, label in STAFF_ROLES],
                     "directory": st == "inactive" and u.is_sales})
    return rows


def customer_roles(u):
    from tenants.models import Membership
    ms = list(u.memberships.select_related("tenant"))
    admin_of = [m.tenant for m in ms if m.role == Membership.Role.TENANT_ADMIN]
    return {"org_admin": u.is_org_admin, "tenant_admin": bool(admin_of), "read_only": u.is_org_readonly,
            "admin_of": admin_of, "memberships": ms}


def customer_rows(org=None, show_archived=False):
    qs = User.objects.filter(kind=Kind.CUSTOMER).select_related("organization")
    if org is not None:
        qs = qs.filter(organization=org)
    users = list(qs.prefetch_related("memberships__tenant").order_by("organization__name", "full_name", "email"))
    ids, emails = _open_invites(users)
    rows = []
    for u in users:
        st = state(u, ids, emails)
        if st == "archived" and not show_archived:
            continue
        r = customer_roles(u)
        rows.append({"u": u, "state": st, "label": STATE_LABELS[st], "org": u.organization, "deletable": never_accepted(u),
                     "roles": [(k, label, r[k]) for k, label in CUSTOMER_ROLES], "info": r})
    # invitations of people who have no account yet
    known = {u.email.lower() for u in users}
    inv = Invitation.objects.open().filter(purpose=Invitation.Purpose.CUSTOMER)
    if org is not None:
        inv = inv.filter(organization=org)
    for i in inv.select_related("organization"):
        if i.email.lower() in known:
            continue
        g = i.grants or {}
        rows.append({"u": None, "invitation": i, "email": i.email, "state": "invited", "label": STATE_LABELS["invited"],
                     "org": i.organization,
                     "roles": [("org_admin", CUSTOMER_ROLES[0][1], bool(g.get("org_admin"))),
                               ("tenant_admin", CUSTOMER_ROLES[1][1],
                                any(v == "tenant_admin" for v in (g.get("tenants") or {}).values())),
                               ("read_only", CUSTOMER_ROLES[2][1], bool(g.get("read_only")))]})
    return rows


# --- actions ---------------------------------------------------------------------------------------------

def set_staff_roles(actor, person, fields):
    """Give a platform user exactly these roles; every role must keep an active holder. Returns errors.

    Correction 16: every super admin's roles can be edited, their own too. The super admin role itself is never
    taken from oneself; taken from another super admin it is only asked for (superadmins.request) — the other roles
    are saved at once. person.sa_removal: that request."""
    from . import roles as R
    from . import superadmins
    fields = set(fields) & {f for f, _l2 in STAFF_ROLES}
    person.sa_removal = None
    remove_sa = person.is_super_admin and "is_super_admin" not in fields
    if remove_sa and person.pk == actor.pk:
        return [_("You cannot take the super admin role from yourself.")]
    if remove_sa and not actor.is_super_admin:
        return [_("Only another super admin can take the super admin role away.")]
    if remove_sa:
        fields.add("is_super_admin")  # stays until the other super admins confirm
    old = {f: getattr(person, f) for f, _l2 in STAFF_ROLES}
    for f, _l2 in STAFF_ROLES:
        setattr(person, f, f in fields)
    people = [person if u.pk == person.pk else u for u in R.staff()]
    before = set(R.missing())
    left = [f for f in R.missing(people) if f not in before or old.get(f)]
    errors = [_("%(r)s: at least one active user must hold it.") % {"r": dict(STAFF_ROLES)[f]} for f in left]
    if errors:
        for f, v in old.items():
            setattr(person, f, v)
        return errors
    person.is_staff = person.is_superuser = person.is_super_admin
    changed = [f for f, v in old.items() if getattr(person, f) != v]
    if changed:
        person.save(update_fields=[f for f, _l2 in STAFF_ROLES] + ["is_staff", "is_superuser"])
        audit(actor, "staff_roles_changed", person, roles=",".join(person.staff_role_labels) or "-")
    if remove_sa:
        person.sa_removal, err = superadmins.request(actor, person)
        if err:
            return [err]
    return []


def set_customer_roles(actor, person, org_admin, read_only, tenant_admin_of):
    """Org admin / read only / tenant admin of the given tenants (of the person's organization)."""
    from tenants.models import Membership
    with transaction.atomic():
        person.is_org_admin, person.is_org_readonly = bool(org_admin), bool(read_only) and not org_admin
        person.save(update_fields=["is_org_admin", "is_org_readonly"])
        keep = {t.pk for t in tenant_admin_of}
        for t in tenant_admin_of:
            Membership.objects.update_or_create(user=person, tenant=t, defaults={"role": Membership.Role.TENANT_ADMIN})
        Membership.objects.filter(user=person, role=Membership.Role.TENANT_ADMIN).exclude(tenant_id__in=keep).delete()
    audit(actor, "member_roles_changed", person, org_admin=person.is_org_admin, read_only=person.is_org_readonly,
          tenant_admin=",".join(str(t) for t in tenant_admin_of))


def block(actor, person):
    from . import roles as R
    if person.pk == actor.pk:
        return _("You cannot block your own account.")
    if person.is_active and person.kind == Kind.STAFF:  # correction 16: every role keeps an active user
        before = set(R.missing())
        person.is_active = False
        left = [f for f in R.missing([person if u.pk == person.pk else u for u in R.staff()]) if f not in before]
        person.is_active = True
        if left:
            return _("%(e)s is the only active user with: %(r)s. Give the role to someone else first.") % {
                "e": person.email, "r": ", ".join(str(dict(STAFF_ROLES)[f]) for f in left)}
    person.is_active = not person.is_active
    person.save(update_fields=["is_active"])
    audit(actor, "user_unblocked" if person.is_active else "user_blocked", person)
    return ""


def clear_roles(person):
    """No roles: an archived person, or a platform user whose invitation is gone (a sales rep directory entry keeps
    its place in the directory)."""
    if person.kind == Kind.STAFF:
        directory = person.is_sales and not person.login_enabled and not person.archived_at
        for f, _l2 in STAFF_ROLES:
            setattr(person, f, False)
        person.is_sales = directory
        person.is_staff = person.is_superuser = False
        person.save(update_fields=[f for f, _l2 in STAFF_ROLES] + ["is_staff", "is_superuser"])
    else:
        person.is_org_admin = person.is_org_readonly = False
        person.save(update_fields=["is_org_admin", "is_org_readonly"])
        person.memberships.all().delete()


def delete(actor, person):
    """Someone who never accepted an invitation is deleted (with the invitations sent), not archived."""
    from django.db.models import ProtectedError
    if person.pk == actor.pk:
        return _("You cannot delete your own account.")
    if not never_accepted(person):
        return _("%(email)s has had an account: archive instead.") % {"email": person.email}
    email = person.email
    try:
        with transaction.atomic():
            Invitation.objects.filter(user=person).delete()
            Invitation.objects.filter(email__iexact=email, accepted_at__isnull=True).delete()
            person.delete()
    except ProtectedError:
        return _("%(email)s is still referred to (an organization or a project): archive instead.") % {"email": email}
    audit(actor, "user_deleted", None, email=email)
    return ""


def archive(actor, person):
    """Archive someone who had an account: no sign-in, no roles, no open invitation; kept for the record."""
    from . import roles as R
    if person.pk == actor.pk:
        return _("You cannot archive your own account.")
    if never_accepted(person):
        return delete(actor, person)
    if person.kind == Kind.STAFF:
        others = [u for u in R.staff() if u.pk != person.pk]
        left = R.missing(others)
        if left:
            return _("Give %(r)s to someone else first.") % {"r": ", ".join(str(dict(STAFF_ROLES)[f]) for f in left)}
    with transaction.atomic():
        person.archived_at, person.is_active, person.login_enabled = timezone.now(), False, False
        person.save(update_fields=["archived_at", "is_active", "login_enabled"])
        Invitation.objects.open().filter(user=person).update(revoked_at=timezone.now())
        Invitation.objects.open().filter(email__iexact=person.email).update(revoked_at=timezone.now())
        clear_roles(person)
    audit(actor, "user_archived", person, email=person.email)
    return ""


def report(request, err, ok):
    if err:
        messages.error(request, err)
    else:
        messages.success(request, ok)

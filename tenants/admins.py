"""Correction 15: the tenant admin — the person on the client's side who gets the tenant's credentials. Always a user of
the tenant's organization; chosen when the tenant is ordered, can be changed at approval and later."""
from accounts.models import Kind, User, audit


def org_users(org):
    """The active users of an organization, its admins first."""
    if org is None:
        return User.objects.none()
    return User.objects.filter(organization=org, kind=Kind.CUSTOMER, is_active=True).order_by(
        "-is_org_admin", "full_name", "email")


def admin_candidates(org):
    """Who can be a tenant admin: the organization's users; for a corporate organization also the platform users
    (corrections-18: platform users can be its users — of a corporate e-mail domain)."""
    from django.db.models import Q
    if org is None:
        return User.objects.none()
    q = Q(organization=org, kind=Kind.CUSTOMER)
    if org.kind == "corp":
        from .forms import corp_domain_ok
        staff = [u.pk for u in User.objects.filter(kind=Kind.STAFF, is_active=True, archived_at__isnull=True)
                 if corp_domain_ok(u)]
        q |= Q(pk__in=staff)
    return User.objects.filter(q, is_active=True).order_by("kind", "-is_org_admin", "full_name", "email")


def label(u):
    return f"{u.full_name or '—'} <{u.email}>"


def choices(org):
    return [(str(u.pk), label(u)) for u in admin_candidates(org)]


def set_tenant_admin(tenant, user, by=None, why=""):
    """Make `user` (a user of the tenant's organization) the tenant admin: kept on the tenant and as its tenant admin
    membership. Returns True when it changed."""
    from .models import Membership
    if user is None or not admin_candidates(tenant.organization).filter(pk=user.pk).exists():
        raise ValueError("the project admin must be a user of the project's organization (corporate: or a platform user)")
    old = tenant.tenant_admin
    Membership.objects.update_or_create(user=user, tenant=tenant, defaults={"role": Membership.Role.TENANT_ADMIN})
    if old is not None and old.pk == user.pk:
        return False
    tenant.tenant_admin = user
    tenant.save(update_fields=["tenant_admin"])
    audit(by, "tenant_admin_set", tenant, user=user.email, old=old.email if old else "", why=why)
    return True


def proposed(tenant):
    """(user or None, why) — who should be the tenant admin when none is set: its only tenant admin member, else the
    organization's first admin, else its only user."""
    from .models import Membership
    members = [m.user for m in Membership.objects.filter(tenant=tenant, role=Membership.Role.TENANT_ADMIN,
                                                         user__is_active=True).select_related("user")
               if m.user.organization_id == tenant.organization_id]
    if len(members) == 1:
        return members[0], "its tenant admin member"
    users = list(org_users(tenant.organization))
    admins = [u for u in users if u.is_org_admin]
    if members:
        org_admin_members = [u for u in members if u.is_org_admin]
        if len(org_admin_members) == 1:
            return org_admin_members[0], "its tenant admin member who is the organization admin"
    if admins:
        return sorted(admins, key=lambda u: u.pk)[0], "the organization admin"
    if users:  # owner 2026-10-29: an organization with users always gives its tenants an admin
        return sorted(users, key=lambda u: u.pk)[0], "an organization user"
    return None, "the organization has no users"


def fill_missing(org, by=None):
    """Give every tenant of the organization without a tenant admin one (owner 2026-10-29). Returns their names."""
    from .models import Tenant
    done = []
    for t in Tenant.objects.filter(organization=org, tenant_admin__isnull=True).exclude(status="archived"):
        if t.is_deleted:
            continue
        u, why = proposed(t)
        if u is not None:
            set_tenant_admin(t, u, by, why=why)
            done.append(t.name)
    return done
